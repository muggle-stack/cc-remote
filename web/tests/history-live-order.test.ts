import assert from "node:assert/strict";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

import type { History, ServerEvent } from "../src/protocol.ts";
import {
  mergeInitialHistory,
  restoreCachedTurnDetails,
  restoreObservedLiveTurnDetails,
} from "../src/history-merge.ts";
import type { Block, Turn } from "../src/reducer.ts";
import { reconcileProvenCompactionOrphans } from
  "../src/compaction-orphans.ts";
import {
  composePastePrompt,
  composerPastePreview,
  countTextLines,
  makeComposerPaste,
  MAX_PROMPT_CHARS,
  PASTE_PREVIEW_CHARS,
} from "../src/composer-pastes.ts";
import {
  activeTurnCandidateIds,
  displayActiveTurnOwnerId,
  exactActiveTurnId,
} from "../src/process-blocks.ts";
import { displayHistoryProjection } from "../src/history-recovery.ts";

const blockIdentity = (block: Block): string => block.kind === "text"
  ? block.message_id
  : block.kind === "tool" ? block.tool_use_id : block.item_id;

const pasteOne = makeComposerPaste("first pasted block", "paste-1");
const pasteTwo = makeComposerPaste("second pasted block", "paste-2");
assert.deepEqual(
  composePastePrompt([pasteOne, pasteTwo], "follow-up"),
  { ok: true, prompt: "first pasted block\n\nsecond pasted block\n\nfollow-up" },
  "paste cards must remain ordered prefixes of the visible composer text",
);
assert.deepEqual(
  composePastePrompt([], "x".repeat(MAX_PROMPT_CHARS)),
  { ok: true, prompt: "x".repeat(MAX_PROMPT_CHARS) },
  "the protocol's exact prompt limit remains sendable",
);
assert.deepEqual(
  composePastePrompt([{ text: "x".repeat(MAX_PROMPT_CHARS) }], "tail"),
  { ok: false, chars: MAX_PROMPT_CHARS + 6, maxChars: MAX_PROMPT_CHARS },
  "paste separators and visible text must participate in the prompt bound",
);

const maximumPaste = `first line\n${"x".repeat(MAX_PROMPT_CHARS - 20)}\nlast`;
assert.equal(countTextLines(maximumPaste), 3,
  "line metadata is computed without changing the complete paste payload");
const maximumPastePreview = composerPastePreview(maximumPaste);
assert.equal(maximumPastePreview.startsWith("first line x"), true);
assert.equal(maximumPastePreview.length <= PASTE_PREVIEW_CHARS, true,
  "a 2 MiB paste contributes only a bounded preview string to the card DOM");
assert.equal(maximumPastePreview.includes("last"), false,
  "preview generation never scans forward to the tail of a huge paste");
assert.equal(exactActiveTurnId([{
  id: "display-owner", clientMsgId: "client-owner",
  historyTurnId: "history-owner",
}], "history-owner", true), "display-owner",
"an exact runtime owner resolves through the displayed History alias");
assert.equal(exactActiveTurnId([{
  id: "collision-a", historyTurnId: "shared-owner",
}, {
  id: "collision-b", historyTurnId: "shared-owner",
}], "shared-owner", true), null,
"colliding aliases fail closed instead of animating either row");
assert.equal(exactActiveTurnId([{
  id: "stale-owner",
}], "stale-owner", false), null,
"an idle lifecycle never revives a stale runtime owner");
assert.deepEqual(activeTurnCandidateIds([{
  id: "collision-a", historyTurnId: "shared-owner",
}, {
  id: "collision-b", historyTurnId: "shared-owner",
}], "shared-owner", true), ["collision-a", "collision-b"],
"only exact aliases of a genuinely active owner become fallback candidates");
assert.deepEqual(activeTurnCandidateIds([{
  id: "stale-owner", historyTurnId: "shared-owner",
}], "shared-owner", false), [],
"idle lifecycle state exposes no ambiguous fallback candidates");
assert.equal(
  displayActiveTurnOwnerId("completed-previous-turn", "new-optimistic-turn"),
  "new-optimistic-turn",
  "a freshly submitted prompt owns the spark before native binding arrives",
);
assert.equal(
  displayActiveTurnOwnerId("still-running-native-turn", null),
  "still-running-native-turn",
  "native ownership remains authoritative when no submit is pending",
);

const settledCachedProcess = restoreCachedTurnDetails([{
  id: "settled-cache-summary", prompt: "done already", done: true,
  detailEventCount: 1, detailLoaded: false, blocks: [],
}], [{
  id: "settled-cache-summary", prompt: "done already", done: true,
  blocks: [{
    kind: "process", item_id: "stale-open-cache-process",
    processKind: "command", phase: "start", status: "running",
    title: "old command", done: false,
  }],
}], "idle")[0];
assert.equal(settledCachedProcess.detailProjection?.blocks[0]?.done, true,
  "a completed summary closes a stale open process restored from IndexedDB");
assert.equal(
  settledCachedProcess.detailProjection?.blocks[0]?.kind === "process"
    ? settledCachedProcess.detailProjection.blocks[0].status : null,
  "succeeded",
  "a stale cached child cannot restart the working spark after refresh",
);

const repairedCachedBackgroundTask = restoreCachedTurnDetails([{
  id: "cached-background-summary", prompt: "run checks", done: true,
  processDetailState: "present", detailEventCount: 2,
  detailLoaded: false, blocks: [],
}], [{
  id: "cached-background-summary", prompt: "run checks", done: true,
  processDetailState: "present", blocks: [{
    kind: "tool", message_id: "cached-bash-message",
    tool_use_id: "cached-bash", tool: "Bash", category: "command",
    input: { command: "make check", run_in_background: true }, done: true,
  }, {
    kind: "process", item_id: "agent:cached-bash",
    processKind: "agent", phase: "end", status: "succeeded",
    parent_id: "cached-bash", title: "协作代理", background: true,
    done: true,
  }],
}], "idle")[0];
const repairedCachedTask = repairedCachedBackgroundTask.detailProjection
  ?.blocks.find((block) => block.kind === "process");
assert.equal(
  repairedCachedTask?.kind === "process"
    ? repairedCachedTask.processKind : null,
  "task",
  "an old background Bash cache cannot retain a fake clickable Agent card",
);
assert.equal(
  repairedCachedTask?.kind === "process" ? repairedCachedTask.title : null,
  "后台任务",
);

const preservedCachedAgent = restoreCachedTurnDetails([{
  id: "cached-agent-summary", prompt: "delegate", done: true,
  processDetailState: "present", detailEventCount: 2,
  detailLoaded: false, blocks: [],
}], [{
  id: "cached-agent-summary", prompt: "delegate", done: true,
  processDetailState: "present", blocks: [{
    kind: "tool", message_id: "cached-agent-message",
    tool_use_id: "cached-agent", tool: "Agent", category: "agent",
    input: {}, done: true,
  }, {
    kind: "process", item_id: "agent:cached-agent",
    processKind: "agent", phase: "end", status: "succeeded",
    parent_id: "cached-agent", title: "真实代理", background: true,
    done: true,
  }],
}], "idle")[0];
assert.equal(
  preservedCachedAgent.detailProjection?.blocks.find(
    (block) => block.kind === "process",
  )?.processKind,
  "agent",
  "targeted cache repair must preserve genuine Agent cards",
);

const cachedNeutralSteerPlan = restoreCachedTurnDetails([{
  id: "cached-neutral-steer", prompt: "continue", done: true,
  detailEventCount: 1, detailLoaded: false, blocks: [],
}], [{
  id: "cached-neutral-steer", prompt: "continue", done: true,
  blocks: [{
    kind: "process", item_id: "cached-neutral-plan",
    processKind: "plan", phase: "update", status: "running",
    title: "计划", plan: [
      { step: "inspect", status: "completed" },
      { step: "finish", status: "inProgress" },
    ], done: false,
  }],
}], "provisional")[0];
const restoredNeutralPlan = cachedNeutralSteerPlan.detailProjection
  ?.blocks[0];
assert.equal(restoredNeutralPlan?.done, false,
  "IndexedDB reconciliation preserves a neutral-steer Plan until an exact terminal");
assert.equal(
  restoredNeutralPlan?.kind === "process" ? restoredNeutralPlan.status : null,
  "running",
  "cache paint cannot fabricate a terminal Plan status",
);

const runningExactPlan = restoreCachedTurnDetails([{
  id: "running-exact-owner", prompt: "continue", done: true,
  detailEventCount: 1, detailLoaded: false, blocks: [],
}], [{
  id: "running-exact-owner", prompt: "continue", done: true,
  blocks: [{
    kind: "process", item_id: "running-exact-plan",
    processKind: "plan", phase: "update", status: "running",
    title: "计划", plan: [{ step: "finish", status: "inProgress" }],
    done: false,
  }],
}], "running", "running-exact-owner")[0].detailProjection?.blocks[0];
assert.equal(runningExactPlan?.done, false,
  "only the exact active owner may preserve its cached open Plan");

const runningOtherPlan = restoreCachedTurnDetails([{
  id: "completed-predecessor", prompt: "old task", done: true,
  detailEventCount: 1, detailLoaded: false, blocks: [],
}], [{
  id: "completed-predecessor", prompt: "old task", done: true,
  blocks: [{
    kind: "process", item_id: "completed-predecessor-plan",
    processKind: "plan", phase: "update", status: "running",
    title: "旧计划", plan: [{ step: "old", status: "inProgress" }],
    done: false,
  }],
}], "running", "new-active-owner")[0].detailProjection?.blocks[0];
assert.equal(runningOtherPlan?.done, true,
  "another running task cannot revive its predecessor's cached Plan");

const settledCachedPlan = restoreCachedTurnDetails([{
  id: "settled-cached-plan", prompt: "finished", done: true,
  detailEventCount: 1, detailLoaded: false, blocks: [],
}], [{
  id: "settled-cached-plan", prompt: "finished", done: true,
  blocks: [{
    kind: "process", item_id: "stale-open-cache-plan",
    processKind: "plan", phase: "update", status: "running",
    title: "计划", plan: [{ step: "finish", status: "inProgress" }],
    done: false,
  }],
}], "idle")[0];
const restoredSettledPlan = settledCachedPlan.detailProjection?.blocks[0];
assert.equal(restoredSettledPlan?.done, true,
  "an authoritative idle boundary settles a delayed cached Plan");
assert.equal(
  restoredSettledPlan?.kind === "process" ? restoredSettledPlan.status : null,
  "succeeded",
  "a delayed same-revision cache cannot restart a settled Plan",
);

const compactionNativeId = "repeated-compaction-native-turn";
const compaction = (item_id: string): Block => ({
  kind: "process", item_id, processKind: "compaction", phase: "end",
  status: "succeeded", turn_id: compactionNativeId,
  title: "压缩上下文", done: true,
});
const compactionOwner: Turn = {
  id: "source-compaction-owner", forkPointId: compactionNativeId,
  prompt: "long task", done: true,
  blocks: [compaction("source-occurrence")],
};
const compactionOrphan = (
  id: string, itemIds: string[],
): Turn => ({
  id, prompt: "", done: true,
  blocks: itemIds.map(compaction),
});
assert.equal(reconcileProvenCompactionOrphans([
  compactionOwner,
  compactionOrphan("different-occurrence-row", ["different-occurrence"]),
]).length, 2,
"the same native task cannot prove two compactions are the same occurrence");
assert.equal(reconcileProvenCompactionOrphans([
  compactionOwner,
  compactionOrphan("exact-occurrence-row", ["source-occurrence"]),
]).length, 1,
"an exact compaction item duplicate remains safely removable");
assert.equal(reconcileProvenCompactionOrphans([
  compactionOwner,
  compactionOrphan("partially-matched-row", [
    "source-occurrence", "bounded-out-occurrence",
  ]),
]).length, 2,
"one item omitted by a bounded summary preserves the complete orphan row");

const staleObservedTurn: Turn = {
  id: "completed-cli-observed", prompt: "inspect", done: true,
  blocks: [{
    kind: "process", item_id: "replayed-cli-command",
    processKind: "command", phase: "start", status: "running",
    title: "retained command", output: "retained output", done: false,
  }],
};
const completedCliSummary: Turn = {
  id: staleObservedTurn.id, prompt: "inspect", done: true,
  blocks: [{ kind: "text", message_id: "completed-cli-final",
    channel: "final", text: "done", done: true }],
};
const preservedBackground = restoreObservedLiveTurnDetails(
  [completedCliSummary], [staleObservedTurn],
)[0];
assert.equal(preservedBackground.detailProjection?.blocks[0]?.done, false,
  "without an idle Codex fence genuine late activity stays live");
assert.equal(staleObservedTurn.blocks[0].done, false,
  "observed-detail restoration never mutates reducer input");

const interleavedLiveBlocks: Block[] = [
  { kind: "text", message_id: "ordered-comment-a", text: "A", done: true,
    channel: "commentary", liveOrder: 0 },
  { kind: "tool", message_id: "ordered-tool-envelope-a",
    tool_use_id: "ordered-tool-a", tool: "Read", input: {}, done: true,
    liveOrder: 1, result: { content: "live A", is_error: false } },
  { kind: "text", message_id: "ordered-comment-b", text: "B", done: true,
    channel: "commentary", liveOrder: 2 },
  { kind: "tool", message_id: "ordered-tool-envelope-b",
    tool_use_id: "ordered-tool-b", tool: "Bash", input: {}, done: false,
    liveOrder: 3 },
];
const interleavedSummaryTurn: Turn = {
  id: "interleaved-turn", prompt: "inspect", done: false,
  blocks: [interleavedLiveBlocks[0], interleavedLiveBlocks[2]].map(
    ({ liveOrder: _liveOrder, ...block }) => block as Block),
};
const interleavedLiveTurn: Turn = {
  ...interleavedSummaryTurn,
  blocks: interleavedLiveBlocks,
};
assert.deepEqual(
  mergeInitialHistory(
    [interleavedSummaryTurn],
    [interleavedLiveTurn],
    { preserveLiveTailOpen: true, reconcileReplayOrphans: true },
  )[0].blocks.map(blockIdentity),
  ["ordered-comment-a", "ordered-tool-a", "ordered-comment-b", "ordered-tool-b"],
  "a running Codex summary subset preserves the complete live chronology",
);

const sourceSuperset = mergeInitialHistory(
  [{
    ...interleavedSummaryTurn,
    blocks: [{ kind: "process", item_id: "source-only-process",
      processKind: "reasoning", phase: "end", status: "succeeded",
      title: "source only", done: true }, ...interleavedSummaryTurn.blocks],
  }],
  [interleavedLiveTurn],
  { preserveLiveTailOpen: true, reconcileReplayOrphans: true },
)[0];
assert.deepEqual(
  sourceSuperset.blocks.map(blockIdentity),
  ["source-only-process", "ordered-comment-a", "ordered-comment-b",
    "ordered-tool-a", "ordered-tool-b"],
  "a History superset remains the chronology authority",
);

const ambiguousLiveOrder = mergeInitialHistory(
  [interleavedSummaryTurn],
  [{ ...interleavedLiveTurn, blocks: interleavedLiveBlocks.map(
    (block) => ({ ...block, liveOrder: 0 })) }],
  { preserveLiveTailOpen: true, reconcileReplayOrphans: true },
)[0];
assert.deepEqual(
  ambiguousLiveOrder.blocks.map(blockIdentity),
  ["ordered-comment-a", "ordered-comment-b", "ordered-tool-a", "ordered-tool-b"],
  "duplicate live order fails closed to the History-first merge",
);

const duplicateHistoryIdentity = mergeInitialHistory(
  [{
    ...interleavedSummaryTurn,
    blocks: [interleavedSummaryTurn.blocks[0],
      { ...interleavedSummaryTurn.blocks[0] },
      interleavedSummaryTurn.blocks[1]],
  }],
  [interleavedLiveTurn],
  { preserveLiveTailOpen: true, reconcileReplayOrphans: true },
)[0];
assert.deepEqual(
  duplicateHistoryIdentity.blocks.map(blockIdentity),
  ["ordered-comment-a", "ordered-comment-a", "ordered-comment-b",
    "ordered-tool-a", "ordered-tool-b"],
  "a duplicate History block identity fails closed instead of disappearing during live-order repair",
);

// A Wrapper generation can restart while the official Codex task keeps
// running in the shared app-server. The browser then owns the user-bound
// pre-restart row while the new generation briefly paints a prompt-less
// suffix. An early idle History fallback can terminalize that suffix before
// the final authoritative page arrives. Once every stable suffix block is
// uniquely owned by one completed native History row, the fallback row is a
// duplicate projection, not a second failed conversation turn.
const restartedNativeTurn = "wrapper-restart-native-turn";
const restartedSettledHistory: Turn = {
  id: "wrapper-restart-history-user",
  prompt: "部署一下",
  forkPointId: restartedNativeTurn,
  done: true,
  blocks: [{
    kind: "text",
    message_id: "wrapper-restart-before-message",
    channel: "commentary",
    text: "开始部署",
    done: true,
  }, {
    kind: "tool",
    message_id: "wrapper-restart-after-envelope",
    tool_use_id: "wrapper-restart-after-tool",
    tool: "shell",
    input: {},
    done: true,
    result: { content: "ok", is_error: false, status: "succeeded" },
  }, {
    kind: "text",
    message_id: "wrapper-restart-final-message",
    channel: "final",
    text: "部署成功",
    done: true,
  }],
};
const restartedTerminalProjection = mergeInitialHistory(
  [restartedSettledHistory],
  [{
    id: "wrapper-restart-history-user",
    prompt: "部署一下",
    forkPointId: restartedNativeTurn,
    done: false,
    blocks: [{
      kind: "text",
      message_id: "wrapper-restart-before-message",
      channel: "commentary",
      text: "开始部署",
      done: true,
    }],
  }, {
    id: "wrapper-restart-promptless-suffix",
    prompt: "",
    liveTaskId: restartedNativeTurn,
    done: true,
    interrupted: true,
    error: "会话已结束，但未收到完整的终止状态。",
    blocks: [{
      kind: "tool",
      message_id: "wrapper-restart-after-envelope",
      tool_use_id: "wrapper-restart-after-tool",
      tool: "shell",
      input: {},
      done: true,
      result: { content: "ok", is_error: true, status: "interrupted" },
    }],
  }],
  {
    newestHistoryId: "wrapper-restart-history-user",
    reconcileReplayOrphans: true,
  },
  true,
);
assert.deepEqual(
  restartedTerminalProjection.map((turn) => ({
    id: turn.id,
    done: turn.done,
    interrupted: turn.interrupted,
    error: turn.error,
    blockIds: turn.blocks.map(blockIdentity),
  })),
  [{
    id: "wrapper-restart-history-user",
    done: true,
    interrupted: undefined,
    error: undefined,
    blockIds: [
      "wrapper-restart-before-message",
      "wrapper-restart-after-tool",
      "wrapper-restart-final-message",
    ],
  }],
  "settled Codex History must absorb a terminalized restart suffix",
);

const repairedPersistedRestartId = mergeInitialHistory(
  [restartedSettledHistory],
  [{
    ...restartedSettledHistory,
    id: "persisted-wrapper-restart-orphan",
    historyTurnId: restartedSettledHistory.id,
    clientMsgId: undefined,
  }],
  { reconcileReplayOrphans: true },
  true,
)[0];
assert.deepEqual(
  {
    id: repairedPersistedRestartId.id,
    historyTurnId: repairedPersistedRestartId.historyTurnId,
  },
  { id: restartedSettledHistory.id, historyTurnId: undefined },
  "a previously cached unowned restart id self-heals to native History",
);

const preservedBrowserOwnedId = mergeInitialHistory(
  [restartedSettledHistory],
  [{
    ...restartedSettledHistory,
    id: "browser-owned-restart-row",
    clientMsgId: "browser-owned-restart-row",
    historyTurnId: restartedSettledHistory.id,
  }],
  { reconcileReplayOrphans: true },
  true,
)[0];
assert.deepEqual(
  {
    id: preservedBrowserOwnedId.id,
    clientMsgId: preservedBrowserOwnedId.clientMsgId,
    historyTurnId: preservedBrowserOwnedId.historyTurnId,
  },
  {
    id: "browser-owned-restart-row",
    clientMsgId: "browser-owned-restart-row",
    historyTurnId: restartedSettledHistory.id,
  },
  "a real browser-owned alias must keep its stable optimistic identity",
);

const uncertainRestartSuffix = mergeInitialHistory(
  [restartedSettledHistory],
  [{
    id: "uncertain-wrapper-restart-suffix",
    prompt: "",
    liveTaskId: restartedNativeTurn,
    done: true,
    interrupted: true,
    error: "会话已结束，但未收到完整的终止状态。",
    blocks: [{
      kind: "tool",
      message_id: "unmatched-restart-envelope",
      tool_use_id: "unmatched-restart-tool",
      tool: "shell",
      input: {},
      done: true,
    }],
  }],
  {
    newestHistoryId: restartedSettledHistory.id,
    reconcileReplayOrphans: true,
  },
  true,
);
assert.deepEqual(
  uncertainRestartSuffix.map((turn) => turn.id),
  [restartedSettledHistory.id, "uncertain-wrapper-restart-suffix"],
  "a restart suffix without unique native block proof must remain visible",
);

const deferredPlan: Block = {
  kind: "process", item_id: "deferred-session-plan", processKind: "plan",
  phase: "snapshot", status: "succeeded", title: "计划", done: true,
  plan: [{ step: "完成修复", status: "inProgress" }],
};
const deferredPlanSummary: Turn = {
  id: "native-plan-summary-turn", clientMsgId: "browser-plan-turn",
  prompt: "继续执行计划。", done: true,
  detailEventCount: 75, detailLoaded: false,
  blocks: [deferredPlan, {
    kind: "text", message_id: "plan-summary-final", channel: "final",
    text: "阶段工作已完成。", done: true,
  }],
};
const repairedPlanOnlyDeferredDetail = mergeInitialHistory(
  [deferredPlanSummary],
  [{
    ...deferredPlanSummary,
    id: "browser-plan-turn",
    historyTurnId: deferredPlanSummary.id,
    detailLoaded: true,
  }],
)[0];
assert.equal(repairedPlanOnlyDeferredDetail.detailLoaded, false,
  "an externalized Plan cannot suppress deferred process history");

const loadedPlanDetail = mergeInitialHistory(
  [{ ...deferredPlanSummary, id: "loaded-plan-detail-turn",
    clientMsgId: undefined }],
  [{
    ...deferredPlanSummary,
    id: "loaded-plan-detail-turn",
    clientMsgId: undefined,
    detailLoaded: true,
    detailProjection: {
      segments: [{
        pageKey: "loaded-plan-detail-page", before: null, events: [],
        hasMore: false, oldestCursor: null, hasNewer: false,
        newerCursor: null, encodedChars: 0,
      }],
      blocks: [{
        kind: "tool", message_id: "loaded-tool-message",
        tool_use_id: "loaded-tool", tool: "Read", input: {}, done: true,
      }],
      capped: false, hasMore: false, oldestCursor: null,
      hasNewer: false, newerCursor: null,
    },
  }],
)[0];
assert.equal(loadedPlanDetail.detailLoaded, true,
  "a real same-turn detail projection remains loaded across summary refresh");

const runningInlineDetail = mergeInitialHistory(
  [{ ...deferredPlanSummary, id: "running-inline-detail", done: false }],
  [{
    ...deferredPlanSummary,
    id: "running-inline-detail",
    done: false,
    detailLoaded: true,
    blocks: [deferredPlan, {
      kind: "tool", message_id: "running-tool-message",
      tool_use_id: "running-tool", tool: "Read", input: {}, done: false,
    }],
  }],
  { preserveLiveTailOpen: true },
)[0];
assert.equal(runningInlineDetail.detailLoaded, true,
  "a raced running summary keeps genuine inline process detail loaded");

const reducerHarness = await createServer({
  root: process.cwd(),
  appType: "custom",
  logLevel: "silent",
  server: { middlewareMode: true, watch: null },
});

try {
  const { createRuntime, initialState, reduce } =
    await reducerHarness.ssrLoadModule("/src/reducer.ts");
  const event = (body: Record<string, unknown>): ServerEvent => ({
    v: 22, ts: 10, ...body,
  } as ServerEvent);

  const compactGapGeneration = "compact-gap-generation";
  const compactGapMessage = "compact-gap-browser-message";
  const compactGapNativeTurn = "compact-gap-native-turn";
  const compactGapContinuation = "compact-gap-continuation";
  const compactGapPrompt = "当前机身的重量是多少？";
  const compactGapRuntime = () => {
    const sid = "compact-gap-pre-history";
    let prepared = {
      ...initialState,
      focusedSid: sid,
      runtimes: { [sid]: createRuntime() },
    };
    prepared = reduce(prepared, { type: "event", event: event({
      type: "replay_start", sid, from_seq: 100, to_seq: 104,
      truncated: true, rebuild: false, generation: compactGapGeneration,
    }) });
    prepared = reduce(prepared, {
      type: "query_sent", sid, msg_id: compactGapMessage,
      prompt: compactGapPrompt, ts: 10_000,
    });
    for (const frame of [
      event({ type: "state", sid, seq: 101, state: "running" }),
      event({ type: "turn_binding", sid, seq: 102,
        msg_id: compactGapMessage, turn_id: compactGapNativeTurn }),
      event({ type: "replay_end", sid, to_seq: 102, truncated: true }),
    ]) {
      prepared = reduce(prepared, { type: "event", event: frame });
    }
    const runtime = prepared.runtimes[sid];
    const question = runtime.turns.find(
      (turn: Turn) => turn.id === compactGapMessage)!;
    assert.equal(runtime.acceptancePending, null,
      "TurnBinding clears transport acceptance before History materializes");
    assert.equal(runtime.pendingLiveBinding?.msgId, compactGapMessage);
    return {
      ...runtime,
      turns: [{ ...question, blocks: [] }, {
        id: compactGapContinuation,
        prompt: "",
        done: false,
        ts: 71_000,
        blocks: [{
          kind: "process" as const,
          item_id: "compact-gap-marker",
          processKind: "compaction" as const,
          phase: "end" as const,
          status: "succeeded" as const,
          turn_id: compactGapContinuation,
          title: "压缩上下文",
          done: true,
        }],
      }],
    };
  };
  const laggingCompactHistory = (
    sid: string, buildSeq: number, inProgress = true, external = false,
    liveSeq = 103,
  ): History => event({
    type: "history", sid, session_id: sid,
    revision: "compact-gap-revision", generation: compactGapGeneration,
    build_seq: buildSeq, live_seq: liveSeq, authoritative: true,
    in_progress: inProgress, external,
    has_more: true, oldest_id: "older-history-turn",
    newest_id: "older-history-turn", detail: "summary", events: [],
    compaction_continuation_turn_ids: [
      compactGapNativeTurn, compactGapContinuation,
    ],
    turns: [{
      id: "older-history-turn", prompt: "上一轮问题",
      blocks: [], done: true, detailEventCount: 0,
      detailLoaded: false, ts: 5_000, doneTs: 6_000,
    }],
  }) as History;
  const applyLaggingCompactHistory = (
    sid: string,
    runtime: ReturnType<typeof createRuntime>,
    inProgress = true,
    external = false,
    liveSeq = 103,
  ) => {
    let candidate = {
      ...initialState,
      focusedSid: sid,
      sessions: [{ session_id: sid, engine: "codex" as const,
        space: "code" as const }],
      runtimes: { [sid]: runtime },
    };
    for (const buildSeq of [1, 2]) {
      candidate = reduce(candidate, { type: "event",
        event: laggingCompactHistory(
          sid, buildSeq, inProgress, external, liveSeq),
      });
    }
    return candidate;
  };
  const hasCompactGapQuestion = (
    runtime: ReturnType<typeof createRuntime>,
  ) => runtime.turns.some((turn: Turn) => turn.id === compactGapMessage);

  const compactGapSid = "compact-gap-keeps-bound-question";
  let compactGapState = applyLaggingCompactHistory(
    compactGapSid, compactGapRuntime());
  let compactGap = compactGapState.runtimes[compactGapSid];
  assert.equal(hasCompactGapQuestion(compactGap), true,
    "a running lagging History keeps the exact bound question visible");
  assert.equal(compactGap.turns.filter((turn: Turn) =>
    turn.id === compactGapMessage).length, 1);
  assert.equal(compactGap.turns.some((turn: Turn) => turn.blocks.some(
    (block: Block) => block.kind === "process"
      && block.processKind === "compaction")), true,
  "retaining the question does not hide the compaction disclosure");
  const compactGapOwners = compactGap.turns.filter((turn: Turn) =>
    turn.id === compactGapMessage
    || turn.blocks.some((block: Block) => block.kind === "process"
      && block.processKind === "compaction"));
  assert.equal(compactGapOwners.length, 1,
    "a proven compact continuation is one active user turn, not two rows");
  assert.equal(compactGap.liveOwner?.turnId, compactGapMessage,
    "the surviving user row remains the exact live narrative owner");
  assert.equal(compactGap.pendingLiveBinding?.msgId, compactGapMessage,
    "a lagging page keeps the exact bridge for later canonical History");

  let continuedCompactGapState = compactGapState;
  for (const frame of [
    event({ type: "assistant_msg_start", sid: compactGapSid, seq: 104,
      message_id: "compact-gap-commentary", channel: "commentary" }),
    event({ type: "delta", sid: compactGapSid, seq: 105,
      message_id: "compact-gap-commentary", channel: "commentary",
      text: "继续检查" }),
    event({ type: "tool_use", sid: compactGapSid, seq: 106,
      message_id: "compact-gap-assistant", tool_use_id: "compact-gap-tool",
      tool: "Read", input: { file_path: "/tmp/example" } }),
  ]) {
    continuedCompactGapState = reduce(continuedCompactGapState, {
      type: "event", event: frame,
    });
  }
  const continuedCompactGap =
    continuedCompactGapState.runtimes[compactGapSid];
  const continuedOwner = continuedCompactGap.turns.find((turn: Turn) =>
    turn.id === compactGapMessage)!;
  assert.ok(continuedOwner.blocks.some((block: Block) =>
    block.kind === "text" && block.message_id === "compact-gap-commentary"));
  assert.ok(continuedOwner.blocks.some((block: Block) =>
    block.kind === "tool" && block.tool_use_id === "compact-gap-tool"));
  assert.equal(continuedCompactGap.turns.filter((turn: Turn) => !turn.prompt
    && turn.blocks.some((block: Block) => block.kind === "process"
      && block.processKind === "compaction")).length, 0,
  "later narrative cannot resurrect a promptless compact duplicate");

  compactGapState = reduce(compactGapState, { type: "event", event: event({
    ...laggingCompactHistory(compactGapSid, 3),
    live_seq: 104,
    newest_id: "compact-gap-history-message",
    turns: [{
      id: "older-history-turn", prompt: "上一轮问题",
      blocks: [], done: true, detailEventCount: 0,
      detailLoaded: false, ts: 5_000, doneTs: 6_000,
    }, {
      id: "compact-gap-history-message",
      clientMsgId: compactGapMessage,
      forkPointId: compactGapNativeTurn,
      prompt: compactGapPrompt,
      blocks: [], done: false, detailEventCount: 0,
      detailLoaded: false, ts: 10_000,
    }],
  }) as History });
  compactGap = compactGapState.runtimes[compactGapSid];
  assert.equal(compactGap.turns.filter((turn: Turn) =>
    turn.prompt === compactGapPrompt).length, 1,
  "canonical History consumes the retained optimistic identity once");
  assert.equal(compactGap.pendingLiveBinding, null,
    "an exact canonical liveOwner hand-off consumes the bridge binding");

  const zeroSeqBinding = compactGapRuntime();
  zeroSeqBinding.pendingLiveBinding = {
    ...zeroSeqBinding.pendingLiveBinding!, seq: 0,
  };
  zeroSeqBinding.lastLiveSeq = 0;
  zeroSeqBinding.lastLifecycleSeq = 0;
  assert.equal(hasCompactGapQuestion(applyLaggingCompactHistory(
    "compact-gap-zero-seq", zeroSeqBinding, true, false, 0).runtimes[
      "compact-gap-zero-seq"]), true,
  "the wrapper reconnect owner seed at sequence zero remains valid");

  const staleBinding = compactGapRuntime();
  staleBinding.pendingLiveBinding = {
    ...staleBinding.pendingLiveBinding!,
    generation: "older-wrapper-generation",
  };
  assert.equal(hasCompactGapQuestion(applyLaggingCompactHistory(
    "compact-gap-stale-binding", staleBinding).runtimes[
      "compact-gap-stale-binding"]), false,
  "a stale-generation binding cannot retain an optimistic question");

  const conflictingNative = compactGapRuntime();
  conflictingNative.turns = conflictingNative.turns.map((turn: Turn) =>
    turn.id === compactGapMessage
      ? { ...turn, forkPointId: "different-native-turn" } : turn);
  assert.equal(hasCompactGapQuestion(applyLaggingCompactHistory(
    "compact-gap-conflicting-native", conflictingNative).runtimes[
      "compact-gap-conflicting-native"]), false,
  "a matching msgId cannot override an explicitly conflicting native owner");

  const switchedGeneration = compactGapRuntime();
  switchedGeneration.historyGeneration = "previous-history-generation";
  const switchedGenerationGap = applyLaggingCompactHistory(
    "compact-gap-new-control-generation", switchedGeneration).runtimes[
      "compact-gap-new-control-generation"];
  assert.equal(hasCompactGapQuestion(switchedGenerationGap), true,
  "current control generation supersedes an old installed History generation");
  assert.equal(switchedGenerationGap.turns.filter((turn: Turn) =>
    turn.id === compactGapMessage
    || turn.blocks.some((block: Block) => block.kind === "process"
      && block.processKind === "compaction")).length, 1,
  "the current control generation can restore its compact continuation owner");
  assert.equal(switchedGenerationGap.liveOwner?.turnId, compactGapMessage);

  const idleGap = applyLaggingCompactHistory(
    "compact-gap-idle", compactGapRuntime(), false).runtimes[
      "compact-gap-idle"];
  assert.equal(hasCompactGapQuestion(idleGap), false,
    "authoritative idle History removes a question absent from its source");
  assert.equal(idleGap.pendingLiveBinding, null);

  const completedGap = compactGapRuntime();
  completedGap.turns = completedGap.turns.map((turn: Turn) =>
    turn.id === compactGapMessage ? { ...turn, done: true } : turn);
  assert.equal(hasCompactGapQuestion(applyLaggingCompactHistory(
    "compact-gap-completed", completedGap).runtimes[
      "compact-gap-completed"]), false,
  "a completed owner cannot be reopened by the running History bridge");

  assert.equal(hasCompactGapQuestion(applyLaggingCompactHistory(
    "compact-gap-external", compactGapRuntime(), true, true).runtimes[
      "compact-gap-external"]), false,
  "external ownership cannot inherit a browser-only pending binding");

  // Production ordering differs from the compact-gap fixture above: a browser
  // can submit and bind its optimistic row first, then reconnect after the
  // compact burst has already pushed that owner out of the bounded replay
  // suffix. ReplayStart must move canonical history aside without deleting the
  // one exact connection-local owner. Otherwise the suffix creates a promptless
  // row, refocus restores the source row beside it, and activity has either zero
  // or two display candidates.
  const submittedGapSid = "submitted-turn-survives-later-compact-gap";
  const submittedGapOtherSid = "submitted-turn-gap-other-session";
  const submittedGapMessage = "submitted-gap-browser-message";
  const submittedGapNativeTurn = "submitted-gap-native-turn";
  const submittedGapContinuation = "submitted-gap-continuation-turn";
  const submittedGapPrompt = "我说的是右边这个盒子。";
  const submittedGapAnswer = "右边盒子的修复已经继续处理。";
  const submittedGapGeneration = "submitted-gap-generation";
  const submittedGapRevision = "submitted-gap-revision";
  const submittedGapBase = createRuntime();
  Object.assign(submittedGapBase, {
    state: "idle",
    syncReady: true,
    turns: [{
      id: "submitted-gap-previous", prompt: "上一轮", blocks: [],
      done: true, ts: 1_000, doneTs: 2_000,
    }],
    hasMore: true,
    oldestId: "submitted-gap-previous",
    historyRevision: submittedGapRevision,
    historyGeneration: submittedGapGeneration,
    controlGeneration: submittedGapGeneration,
    historyBuildSeq: 7,
    historyLiveSeq: 99,
    historyNewestId: "submitted-gap-previous",
  });
  let submittedGapState = {
    ...initialState,
    focusedSid: submittedGapSid,
    sessions: [{
      session_id: submittedGapSid,
      engine: "codex" as const,
      space: "code" as const,
    }, {
      session_id: submittedGapOtherSid,
      engine: "codex" as const,
      space: "code" as const,
    }],
    runtimes: {
      [submittedGapSid]: submittedGapBase,
      [submittedGapOtherSid]: createRuntime(),
    },
  };
  submittedGapState = reduce(submittedGapState, {
    type: "query_sent", sid: submittedGapSid,
    msg_id: submittedGapMessage, prompt: submittedGapPrompt, ts: 10_000,
  });
  for (const frame of [
    event({ type: "state", sid: submittedGapSid, seq: 100,
      state: "running" }),
    event({ type: "turn_binding", sid: submittedGapSid, seq: 101,
      msg_id: submittedGapMessage, turn_id: submittedGapNativeTurn }),
  ]) {
    submittedGapState = reduce(submittedGapState, {
      type: "event", event: frame,
    });
  }
  const submittedGapBeforeReplay = submittedGapState;
  const preexistingRecoveryDuplicate = reduce({
    ...submittedGapBeforeReplay,
    historyRecovery: {
      sid: submittedGapSid,
      turns: [{
        id: "submitted-gap-frozen-history-row",
        clientMsgId: submittedGapMessage,
        prompt: submittedGapPrompt,
        blocks: [], done: false, ts: 10_000,
      }],
      activeOwnerId: null,
      hasMore: true,
      oldestId: "submitted-gap-previous",
      viewRevision: submittedGapRevision,
      expectedGeneration: submittedGapGeneration,
      candidateBuildSeq: 7,
      acceptedRevision: null,
    },
  }, {
    type: "event", event: event({
      type: "replay_start", sid: submittedGapSid,
      generation: submittedGapGeneration,
      from_seq: 102, to_seq: 106, truncated: true, rebuild: false,
    }),
  });
  const reconciledRecoveryView = displayHistoryProjection(
    preexistingRecoveryDuplicate.historyRecovery,
    submittedGapSid,
    preexistingRecoveryDuplicate.runtimes[submittedGapSid],
  );
  assert.equal(reconciledRecoveryView.turns.filter((turn: Turn) =>
    turn.prompt === submittedGapPrompt).length, 1,
  "a repeated replay envelope collapses canonical and optimistic recovery rows");
  assert.equal(activeTurnCandidateIds(
    reconciledRecoveryView.turns,
    reconciledRecoveryView.activeOwnerId,
    true,
  ).length, 1,
  "a reconciled recovery duplicate exposes one spark candidate");
  submittedGapState = reduce(submittedGapState, {
    type: "event", event: event({
      type: "replay_start", sid: submittedGapSid,
      generation: submittedGapGeneration,
      from_seq: 102, to_seq: 106, truncated: true, rebuild: false,
    }),
  });
  assert.equal(
    submittedGapState.runtimes[submittedGapSid].turns.filter(
      (turn: Turn) => turn.id === submittedGapMessage
        && turn.prompt === submittedGapPrompt).length,
    1,
    "a later replay gap must not erase the already-bound optimistic question",
  );
  const submittedGapAfterReplay =
    submittedGapState.runtimes[submittedGapSid];
  assert.deepEqual(activeTurnCandidateIds(
    submittedGapAfterReplay.turns,
    displayActiveTurnOwnerId(
      submittedGapAfterReplay.liveOwner?.turnId,
      submittedGapAfterReplay.acceptancePending,
    ),
    submittedGapAfterReplay.state !== "idle"
      || !!submittedGapAfterReplay.acceptancePending,
  ), [submittedGapMessage],
  "replay recovery must keep the submitted row as the sole running spark owner");
  const submittedRecoveryView = displayHistoryProjection(
    submittedGapState.historyRecovery,
    submittedGapSid,
    submittedGapAfterReplay,
    submittedGapState.historyBrowse,
    submittedGapState.retainedHistoryBrowse,
  );
  assert.equal(submittedRecoveryView.turns.filter((turn: Turn) =>
    turn.prompt === submittedGapPrompt).length, 1,
  "the frozen recovery view must keep exactly one copy of the submitted question");
  assert.deepEqual(activeTurnCandidateIds(
    submittedRecoveryView.turns,
    submittedRecoveryView.activeOwnerId,
    submittedRecoveryView.recovering
      && submittedGapAfterReplay.state !== "idle",
  ), [submittedGapMessage],
  "the visible recovery projection keeps the exact running spark owner");

  let refocusedDuringRecovery = reduce(submittedGapState, {
    type: "focus_session", sid: submittedGapOtherSid,
  });
  refocusedDuringRecovery = reduce(refocusedDuringRecovery, {
    type: "focus_session", sid: submittedGapSid,
  });
  const refocusedDuringRuntime =
    refocusedDuringRecovery.runtimes[submittedGapSid];
  const refocusedDuringView = displayHistoryProjection(
    refocusedDuringRecovery.historyRecovery,
    submittedGapSid,
    refocusedDuringRuntime,
    refocusedDuringRecovery.historyBrowse,
    refocusedDuringRecovery.retainedHistoryBrowse,
  );
  assert.equal(refocusedDuringView.turns.filter((turn: Turn) =>
    turn.prompt === submittedGapPrompt).length, 1,
  "switching away and back mid-recovery cannot duplicate the question");
  assert.deepEqual(activeTurnCandidateIds(
    refocusedDuringView.turns,
    displayActiveTurnOwnerId(
      refocusedDuringRuntime.liveOwner?.turnId,
      refocusedDuringRuntime.acceptancePending,
    ),
    refocusedDuringRuntime.state !== "idle"
      || !!refocusedDuringRuntime.acceptancePending,
  ), [submittedGapMessage],
  "switching away and back mid-recovery keeps one visible spark owner");
  const crossGenerationGap = reduce(submittedGapBeforeReplay, {
    type: "event", event: event({
      type: "replay_start", sid: submittedGapSid,
      generation: "submitted-gap-replacement-generation",
      from_seq: 1, to_seq: 2, truncated: true, rebuild: false,
    }),
  }).runtimes[submittedGapSid];
  assert.equal(crossGenerationGap.turns.some((turn: Turn) =>
    turn.id === submittedGapMessage), false,
  "a different wrapper generation cannot inherit the old local owner");
  const rebuiltSubmittedGap = reduce(submittedGapBeforeReplay, {
    type: "event", event: event({
      type: "replay_start", sid: submittedGapSid,
      generation: submittedGapGeneration,
      from_seq: 1, to_seq: 2, truncated: false, rebuild: true,
    }),
  }).runtimes[submittedGapSid];
  assert.equal(rebuiltSubmittedGap.turns.some((turn: Turn) =>
    turn.id === submittedGapMessage), false,
  "a wrapper rebuild still reconstructs narrative solely from source");
  for (const frame of [
    event({ type: "process", sid: submittedGapSid, seq: 102,
      item_id: "submitted-gap-compaction", kind: "compaction", phase: "end",
      status: "succeeded", turn_id: submittedGapContinuation,
      title: "压缩上下文" }),
    event({ type: "assistant_msg_start", sid: submittedGapSid, seq: 103,
      message_id: "submitted-gap-live-answer", channel: "final" }),
    event({ type: "delta", sid: submittedGapSid, seq: 104,
      message_id: "submitted-gap-live-answer", channel: "final",
      text: submittedGapAnswer }),
    event({ type: "assistant_msg_end", sid: submittedGapSid, seq: 105,
      message_id: "submitted-gap-live-answer", channel: "final" }),
    event({ type: "replay_end", sid: submittedGapSid, to_seq: 105,
      truncated: true }),
  ]) {
    submittedGapState = reduce(submittedGapState, {
      type: "event", event: frame,
    });
  }
  const submittedLaggingHistory = (buildSeq: number): History => event({
    type: "history", sid: submittedGapSid, session_id: submittedGapSid,
    revision: submittedGapRevision, generation: submittedGapGeneration,
    build_seq: buildSeq, live_seq: 105, authoritative: true,
    in_progress: true, external: false, has_more: true,
    oldest_id: "submitted-gap-previous",
    newest_id: "submitted-gap-previous", detail: "summary", events: [],
    compaction_continuation_turn_ids: [
      submittedGapNativeTurn, submittedGapContinuation,
    ],
    turns: [{
      id: "submitted-gap-previous", prompt: "上一轮", blocks: [],
      done: true, detailEventCount: 0, detailLoaded: false,
      ts: 1_000, doneTs: 2_000,
    }],
  }) as History;
  for (const buildSeq of [8, 9]) {
    submittedGapState = reduce(submittedGapState, {
      type: "event", event: submittedLaggingHistory(buildSeq),
    });
  }
  const submittedCanonicalHistory = (buildSeq: number): History => event({
    type: "history", sid: submittedGapSid, session_id: submittedGapSid,
    revision: submittedGapRevision, generation: submittedGapGeneration,
    build_seq: buildSeq, live_seq: 105, authoritative: true,
    in_progress: true, external: false, has_more: true,
    oldest_id: "submitted-gap-previous",
    newest_id: "submitted-gap-history-message", detail: "summary", events: [],
    compaction_continuation_turn_ids: [
      submittedGapNativeTurn, submittedGapContinuation,
    ],
    turns: [{
      id: "submitted-gap-previous", prompt: "上一轮", blocks: [],
      done: true, detailEventCount: 0, detailLoaded: false,
      ts: 1_000, doneTs: 2_000,
    }, {
      id: "submitted-gap-history-message",
      clientMsgId: submittedGapMessage,
      forkPointId: submittedGapNativeTurn,
      prompt: submittedGapPrompt,
      blocks: [{
        kind: "text", message_id: "submitted-gap-history-answer",
        channel: "final", text: submittedGapAnswer, done: true,
      }],
      done: false, detailEventCount: 2, detailLoaded: false, ts: 10_000,
    }],
  }) as History;
  submittedGapState = reduce(submittedGapState, {
    type: "event", event: submittedCanonicalHistory(10),
  });
  submittedGapState = reduce(submittedGapState, {
    type: "focus_session", sid: submittedGapOtherSid,
  });
  submittedGapState = reduce(submittedGapState, {
    type: "focus_session", sid: submittedGapSid,
  });
  submittedGapState = reduce(submittedGapState, {
    type: "event", event: submittedCanonicalHistory(11),
  });
  const submittedRecovered = submittedGapState.runtimes[submittedGapSid];
  const submittedRows = submittedRecovered.turns.filter(
    (turn: Turn) => turn.prompt === submittedGapPrompt);
  assert.equal(submittedRows.length, 1,
    "canonical refocus history replaces rather than duplicates the live row");
  assert.equal(submittedRows[0].blocks.filter((block: Block) =>
    block.kind === "text" && block.text === submittedGapAnswer).length, 1,
  "regenerated History/live assistant ids still render the answer once");
  const submittedOwner = displayActiveTurnOwnerId(
    submittedRecovered.liveOwner?.turnId,
    submittedRecovered.acceptancePending,
  );
  assert.deepEqual(activeTurnCandidateIds(
    submittedRecovered.turns,
    submittedOwner,
    submittedRecovered.state !== "idle"
      || !!submittedRecovered.acceptancePending,
  ), [submittedRows[0].id],
  "refocus leaves exactly one display row for the still-running spark");

  const rollbackGapSid = "compact-gap-real-rollback";
  const rollbackGap = reduce({ ...initialState, focusedSid: rollbackGapSid,
    runtimes: { [rollbackGapSid]: compactGapRuntime() },
  }, { type: "event", event: event({
    type: "history_invalidated", sid: rollbackGapSid,
    session_id: rollbackGapSid, revision: "compact-gap-after-rollback",
    reason: "rollback",
  }) }).runtimes[rollbackGapSid];
  assert.deepEqual(rollbackGap.turns, [],
    "a real source rollback remains destructive");
  assert.equal(rollbackGap.pendingLiveBinding, null);

  const sid = "history-live-block-order";
  let state = {
    ...initialState,
    focusedSid: sid,
    sessions: [{
      session_id: sid,
      engine: "codex" as const,
      space: "code" as const,
    }],
    runtimes: { [sid]: createRuntime() },
  };

  const sparkSid = "new-submit-spark-owner";
  const settledRuntime = createRuntime();
  settledRuntime.state = "idle";
  settledRuntime.turns = [{
    id: "settled-previous-turn", prompt: "old", blocks: [], done: true,
  }];
  settledRuntime.liveOwner = { turnId: "settled-previous-turn", seq: 20 };
  const submittedSparkState = reduce({
    ...initialState,
    focusedSid: sparkSid,
    runtimes: { [sparkSid]: settledRuntime },
  }, {
    type: "query_sent", sid: sparkSid, prompt: "new", msg_id: "new-turn",
    ts: 21_000,
  });
  const submittedSparkRuntime = submittedSparkState.runtimes[sparkSid];
  assert.deepEqual(activeTurnCandidateIds(
    submittedSparkRuntime.turns,
    displayActiveTurnOwnerId(
      submittedSparkRuntime.liveOwner?.turnId,
      submittedSparkRuntime.acceptancePending,
    ),
    submittedSparkRuntime.state !== "idle"
      || !!submittedSparkRuntime.acceptancePending,
  ), ["new-turn"],
  "query_sent moves the spark directly to the optimistic tail row");

  for (const liveEvent of [
    event({ type: "state", sid, state: "running", seq: 1 }),
    event({ type: "user_msg", sid,
      msg_id: "ordered-live-turn", prompt: "inspect", seq: 2 }),
    event({ type: "assistant_msg_start", sid,
      message_id: "ordered-live-comment-a", channel: "commentary", seq: 3 }),
    event({ type: "delta", sid,
      message_id: "ordered-live-comment-a", channel: "commentary",
      text: "live A", seq: 4 }),
    event({ type: "assistant_msg_end", sid,
      message_id: "ordered-live-comment-a", channel: "commentary", seq: 5 }),
    event({ type: "tool_use", sid,
      message_id: "ordered-live-tool-envelope-a",
      tool_use_id: "ordered-live-tool-a", tool: "Read",
      input: { file_path: "/tmp/a" }, seq: 6 }),
    event({ type: "tool_result", sid,
      tool_use_id: "ordered-live-tool-a", content: "live result A",
      is_error: false, seq: 7 }),
    event({ type: "assistant_msg_start", sid,
      message_id: "ordered-live-comment-b", channel: "commentary", seq: 8 }),
    event({ type: "delta", sid,
      message_id: "ordered-live-comment-b", channel: "commentary",
      text: "live B", seq: 9 }),
    event({ type: "assistant_msg_end", sid,
      message_id: "ordered-live-comment-b", channel: "commentary", seq: 10 }),
    event({ type: "tool_use", sid,
      message_id: "ordered-live-tool-envelope-b",
      tool_use_id: "ordered-live-tool-b", tool: "Bash",
      input: { command: "pwd" }, seq: 11 }),
  ]) {
    state = reduce(state, { type: "event", event: liveEvent });
  }
  state = reduce(state, {
    type: "event",
    event: event({
      type: "history",
      sid,
      session_id: sid,
      revision: "ordered-live-r1",
      generation: "ordered-live-g1",
      build_seq: 1,
      live_seq: 11,
      detail: "summary",
      has_more: false,
      in_progress: true,
      events: [],
      turns: [{
        id: "ordered-live-turn",
        prompt: "inspect",
        done: false,
        blocks: [
          { kind: "text", message_id: "ordered-live-comment-a",
            channel: "commentary", text: "canonical A", done: true },
          { kind: "text", message_id: "ordered-live-comment-b",
            channel: "commentary", text: "canonical B", done: true },
        ],
      }],
    }),
  });

  const blocks: Block[] = state.runtimes[sid].turns[0].blocks;
  assert.deepEqual(
    blocks.map(blockIdentity),
    ["ordered-live-comment-a", "ordered-live-tool-a",
      "ordered-live-comment-b", "ordered-live-tool-b"],
    "the real History reducer preserves complete live tool chronology",
  );
  assert.equal(
    blocks[0].kind === "text" ? blocks[0].text : null,
    "canonical A",
    "chronology restoration must retain History text payload authority",
  );
  const toolResult = blocks[1].kind === "tool" ? blocks[1].result : null;
  assert.equal(toolResult?.content, "live result A",
    "chronology restoration must retain the live tool result");
  assert.equal(toolResult?.is_error, false,
    "chronology restoration must retain the live tool lifecycle");

  const idleSid = "codex-shared-cli-idle-projection";
  const idleRevision = "codex-shared-cli-idle-r1";
  const idleGeneration = "codex-shared-cli-idle-g1";
  const openObserved = (): Turn => ({
    id: "shared-cli-completed-row", prompt: "done already", done: true,
    detailEventCount: 1,
    blocks: [{ kind: "process", item_id: "shared-cli-replayed-process",
      processKind: "command", phase: "start", status: "running",
      title: "CLI replay", output: "keep this output", done: false }],
  });
  const summaryTurn = {
    id: "shared-cli-completed-row", prompt: "done already", done: true,
    detailEventCount: 1, detailLoaded: false,
    blocks: [{ kind: "text" as const, message_id: "shared-cli-final",
      channel: "final" as const, text: "complete", done: true }],
  };
  const idleHistory = event({
    type: "history", sid: idleSid, session_id: idleSid,
    revision: idleRevision, generation: idleGeneration,
    build_seq: 2, live_seq: 40, authoritative: true,
    detail: "summary", in_progress: false, has_more: false,
    newest_id: summaryTurn.id, events: [], turns: [summaryTurn],
  });
  let idleState = {
    ...initialState,
    focusedSid: idleSid,
    sessions: [{ session_id: idleSid, engine: "codex" as const }],
    runtimes: {
      [idleSid]: {
        ...createRuntime(), state: "idle" as const,
        historyRevision: idleRevision, historyGeneration: idleGeneration,
        lastLiveSeq: 40, liveDetailTurnIds: [summaryTurn.id],
        turns: [openObserved()],
      },
    },
  };
  idleState = reduce(idleState, { type: "event", event: idleHistory });
  const repaired = idleState.runtimes[idleSid].turns[0];
  const repairedProcess = repaired.detailProjection?.blocks.find(
    (block: Block) => block.kind === "process");
  assert.equal(idleState.runtimes[idleSid].state, "idle");
  assert.equal(repairedProcess?.done, true,
    "a passive shared CLI holder cannot reactivate completed Codex detail");
  assert.equal(repairedProcess?.kind === "process"
    ? repairedProcess.output : null, "keep this output",
  "idle History repair retains the collapsed process payload");
  const { ChatView } = await reducerHarness.ssrLoadModule(
    "/src/components/ChatView.tsx");
  const idleMarkup = renderToStaticMarkup(createElement(ChatView, {
    sid: idleSid, turns: [repaired], engine: "codex",
    onEdit: () => {}, onGetDiff: () => {}, onLoadDetail: () => {},
  }));
  assert.doesNotMatch(idleMarkup, /class="turn-working"/,
    "a passive CLI holder cannot leave the working spark animated");
  assert.match(idleMarkup, /class="turn-done-mark"/,
    "the completed row returns to its static terminal spark");
  assert.match(idleMarkup, /aria-expanded="false"/,
    "the completed process disclosure stays settled instead of flapping open");

  const claudeTerminalSid = "claude-terminal-restored-foreground";
  const claudeTerminalTurnId = "claude-terminal-user";
  const claudeTerminalRevision = "claude-terminal-r1";
  const claudeTerminalGeneration = "claude-terminal-g1";
  let claudeTerminalState = {
    ...initialState,
    focusedSid: claudeTerminalSid,
    sessions: [{
      session_id: claudeTerminalSid,
      engine: "claude" as const,
      space: "code" as const,
    }],
    runtimes: { [claudeTerminalSid]: {
      ...createRuntime(),
      state: "idle" as const,
      historyRevision: claudeTerminalRevision,
      historyGeneration: claudeTerminalGeneration,
      lastLiveSeq: 80,
      liveDetailTurnIds: [claudeTerminalTurnId],
      turns: [{
        id: claudeTerminalTurnId,
        prompt: "inspect",
        done: true,
        doneTs: 20_000,
        forkPointId: "claude-terminal-assistant",
        detailEventCount: 2,
        blocks: [{
          kind: "tool" as const,
          message_id: "claude-open-tool-message",
          tool_use_id: "claude-open-tool",
          tool: "Bash",
          input: { command: "tail output" },
          output: "retained output",
          done: false,
        }, {
          kind: "process" as const,
          item_id: "claude-background-agent",
          processKind: "agent" as const,
          phase: "update" as const,
          status: "running" as const,
          title: "background agent",
          background: true,
          done: false,
        }],
      }],
    } },
  };
  claudeTerminalState = reduce(claudeTerminalState, {
    type: "event",
    event: event({
      type: "history",
      sid: claudeTerminalSid,
      session_id: claudeTerminalSid,
      revision: claudeTerminalRevision,
      generation: claudeTerminalGeneration,
      build_seq: 2,
      live_seq: 80,
      authoritative: true,
      detail: "summary",
      in_progress: false,
      has_more: false,
      newest_id: claudeTerminalTurnId,
      events: [],
      turns: [{
        id: claudeTerminalTurnId,
        prompt: "inspect",
        done: true,
        doneTs: 20_000,
        forkPointId: "claude-terminal-assistant",
        detailEventCount: 2,
        detailLoaded: false,
        blocks: [{
          kind: "text",
          message_id: "claude-terminal-final",
          channel: "final",
          text: "complete",
          done: true,
        }],
      }],
    }),
  });
  const repairedClaudeTurn =
    claudeTerminalState.runtimes[claudeTerminalSid].turns[0];
  const repairedClaudeTool = repairedClaudeTurn.detailProjection?.blocks.find(
    (block: Block) => block.kind === "tool");
  const preservedClaudeAgent = repairedClaudeTurn.detailProjection?.blocks.find(
    (block: Block) => block.kind === "process"
      && block.background === true);
  assert.equal(repairedClaudeTool?.done, true,
    "an authoritative Claude terminal settles restored foreground tools");
  assert.equal(preservedClaudeAgent?.done, false,
    "an authoritative parent terminal preserves detached background Agent work");

  const loadedClaudeSid = "claude-terminal-loaded-background";
  let loadedClaudeState = {
    ...initialState,
    focusedSid: loadedClaudeSid,
    sessions: [{
      session_id: loadedClaudeSid,
      engine: "claude" as const,
      space: "code" as const,
    }],
    runtimes: { [loadedClaudeSid]: {
      ...createRuntime(),
      state: "idle" as const,
      historyRevision: "claude-loaded-r1",
      historyGeneration: "claude-loaded-g1",
      lastLiveSeq: 12,
      turns: [{
        id: "claude-loaded-turn", prompt: "delegate", done: true,
        detailLoaded: true, blocks: [],
        detailProjection: {
          segments: [{
            pageKey: "claude-loaded-page", before: null, events: [],
            hasMore: false, oldestCursor: null, hasNewer: false,
            newerCursor: null, encodedChars: 0,
          }],
          blocks: [{
            kind: "process" as const,
            item_id: "claude-loaded-agent",
            processKind: "agent" as const,
            phase: "update" as const,
            status: "running" as const,
            title: "detached agent",
            background: true,
            done: false,
          }],
          capped: false, hasMore: false, oldestCursor: null,
          hasNewer: false, newerCursor: null,
        },
      }],
    } },
  };
  loadedClaudeState = reduce(loadedClaudeState, {
    type: "event",
    event: event({
      type: "history",
      sid: loadedClaudeSid,
      session_id: loadedClaudeSid,
      revision: "claude-loaded-r1",
      generation: "claude-loaded-g1",
      build_seq: 2,
      live_seq: 12,
      authoritative: true,
      detail: "summary",
      in_progress: false,
      has_more: false,
      newest_id: "claude-loaded-turn",
      events: [],
      turns: [{
        id: "claude-loaded-turn", prompt: "delegate", done: true,
        detailEventCount: 1, detailLoaded: false, blocks: [],
      }],
    }),
  });
  assert.equal(loadedClaudeState.runtimes[loadedClaudeSid].turns[0]
    .detailProjection?.blocks[0]?.done, false,
  "a settled summary preserves an already-loaded detached Agent projection");

  const repairedClaudeMarkup = renderToStaticMarkup(createElement(ChatView, {
    sid: claudeTerminalSid,
    turns: [repairedClaudeTurn],
    engine: "claude",
    onEdit: () => {},
    onGetDiff: () => {},
    onFork: () => {},
  }));
  assert.doesNotMatch(repairedClaudeMarkup, /class="turn-working"/);
  assert.match(repairedClaudeMarkup, /class="ubub-meta ai-meta"/,
    "the completed Claude reply regains its completion footer");
  assert.match(repairedClaudeMarkup, /aria-label="复制"/);
  assert.match(repairedClaudeMarkup, /aria-label="派生"/);
  assert.match(repairedClaudeMarkup, /class="turn-done-mark"/);

  const shortClaudeSid = "claude-short-known-head";
  let shortClaudeState = {
    ...initialState,
    focusedSid: shortClaudeSid,
    sessions: [{
      session_id: shortClaudeSid,
      engine: "claude" as const,
      space: "code" as const,
    }],
    runtimes: { [shortClaudeSid]: {
      ...createRuntime(), loading: true,
    } },
  };
  shortClaudeState = reduce(shortClaudeState, {
    type: "event",
    event: event({
      type: "history",
      sid: shortClaudeSid,
      session_id: shortClaudeSid,
      revision: "claude-short-r1",
      generation: "claude-short-g1",
      build_seq: 1,
      live_seq: 0,
      authoritative: true,
      detail: "summary",
      in_progress: false,
      has_more: false,
      events: [],
      turns: [],
    }),
  });
  assert.equal(
    shortClaudeState.runtimes[shortClaudeSid].historyHeadKnown,
    true,
    "an authoritative empty/short head records that history is already at its start",
  );
  shortClaudeState = reduce(shortClaudeState, {
    type: "focus_session", sid: "another-short-session",
  });
  shortClaudeState = reduce(shortClaudeState, {
    type: "focus_session", sid: shortClaudeSid,
  });
  assert.equal(shortClaudeState.runtimes[shortClaudeSid].loading, false,
    "refocusing a known history start cannot flash the initial history loader");

  const restartedShortClaudeState = reduce(shortClaudeState, {
    type: "event",
    event: event({
      type: "snapshot",
      sid: shortClaudeSid,
      cc_session_id: shortClaudeSid,
      generation: "claude-short-g2",
      state: "idle",
    }),
  });
  assert.equal(
    restartedShortClaudeState.runtimes[shortClaudeSid].historyHeadKnown,
    false,
    "a new wrapper generation revokes the previous empty-head proof",
  );
  assert.equal(
    restartedShortClaudeState.runtimes[shortClaudeSid].loading,
    true,
    "an empty session waits for new-generation History after wrapper restart",
  );

  const staleCacheSid = "claude-empty-stale-cache-generation";
  let staleCacheState = {
    ...initialState,
    focusedSid: staleCacheSid,
    sessions: [{
      session_id: staleCacheSid,
      engine: "claude" as const,
      space: "code" as const,
    }],
    runtimes: { [staleCacheSid]: {
      ...createRuntime(), loading: true,
    } },
  };
  staleCacheState = reduce(staleCacheState, {
    type: "event",
    event: event({
      type: "snapshot",
      sid: staleCacheSid,
      cc_session_id: staleCacheSid,
      generation: "claude-cache-live-g2",
      state: "idle",
    }),
  });
  staleCacheState = reduce(staleCacheState, {
    type: "hydrate_cache",
    sid: staleCacheSid,
    turns: [],
    revision: "claude-cache-old-r1",
    generation: "claude-cache-old-g1",
    historyAtStart: true,
  });
  assert.deepEqual({
    controlGeneration:
      staleCacheState.runtimes[staleCacheSid].controlGeneration,
    historyGeneration:
      staleCacheState.runtimes[staleCacheSid].historyGeneration,
    historyHeadKnown:
      staleCacheState.runtimes[staleCacheSid].historyHeadKnown,
    loading: staleCacheState.runtimes[staleCacheSid].loading,
  }, {
    controlGeneration: "claude-cache-live-g2",
    historyGeneration: null,
    historyHeadKnown: false,
    loading: true,
  }, "a paint-only empty cache cannot roll a live generation backwards");
  const staleCacheFallback = reduce(staleCacheState, {
    type: "hydrate_cache",
    sid: staleCacheSid,
    turns: [],
    revision: null,
  });
  assert.equal(staleCacheFallback.runtimes[staleCacheSid].loading, false,
    "the cache timeout fallback still releases an otherwise silent loader");

  const rekeyTempSid = "tmp-known-empty-head";
  const rekeyRealSid = "claude-known-empty-head";
  const rekeyedKnownHead = reduce({
    ...initialState,
    focusedSid: rekeyTempSid,
    sessions: [{
      session_id: rekeyTempSid,
      engine: "claude" as const,
      space: "code" as const,
    }],
    runtimes: {
      [rekeyTempSid]: {
        ...createRuntime(), loading: true,
        controlGeneration: "claude-rekey-g1",
      },
      [rekeyRealSid]: {
        ...createRuntime(), loading: false,
        controlGeneration: "claude-rekey-g1",
        historyGeneration: "claude-rekey-g1",
        historyRevision: "claude-rekey-r1",
        historyHeadKnown: true,
      },
    },
  }, {
    type: "event",
    event: event({
      type: "session_rekey",
      sid: rekeyTempSid,
      old_key: rekeyTempSid,
      session_id: rekeyRealSid,
    }),
  });
  assert.deepEqual({
    historyRevision:
      rekeyedKnownHead.runtimes[rekeyRealSid].historyRevision,
    historyGeneration:
      rekeyedKnownHead.runtimes[rekeyRealSid].historyGeneration,
    historyHeadKnown:
      rekeyedKnownHead.runtimes[rekeyRealSid].historyHeadKnown,
    loading: rekeyedKnownHead.runtimes[rekeyRealSid].loading,
  }, {
    historyRevision: "claude-rekey-r1",
    historyGeneration: "claude-rekey-g1",
    historyHeadKnown: true,
    loading: false,
  }, "rekey keeps empty-head readiness with the selected History authority");

  shortClaudeState = reduce(shortClaudeState, {
    type: "event",
    event: event({
      type: "history_invalidated",
      sid: shortClaudeSid,
      session_id: shortClaudeSid,
      revision: "claude-short-r2",
      reason: "rollback",
    }),
  });
  assert.equal(
    shortClaudeState.runtimes[shortClaudeSid].historyHeadKnown,
    false,
    "a rollback revokes the old history-start proof",
  );
  assert.equal(shortClaudeState.runtimes[shortClaudeSid].loading, true,
    "an invalidated empty projection keeps the truthful cold-history loader");

  const repairedPlanOnlyMarkup = renderToStaticMarkup(createElement(ChatView, {
    sid: "repaired-plan-summary-session",
    turns: [repairedPlanOnlyDeferredDetail],
    engine: "codex", onEdit: () => {}, onGetDiff: () => {},
    onLoadDetail: () => {},
    externalPlanProgress: {
      turnId: repairedPlanOnlyDeferredDetail.id,
      itemId: deferredPlan.kind === "process" ? deferredPlan.item_id : "",
    },
  }));
  assert.match(repairedPlanOnlyMarkup, /已处理/,
    "lifting a Plan into the session strip retains the process disclosure");
  assert.match(repairedPlanOnlyMarkup, /75 项/);

  const directAnswerMarkup = renderToStaticMarkup(createElement(ChatView, {
    sid: "direct-answer-session",
    turns: [{
      id: "direct-answer-turn", prompt: "你好", done: true,
      detailEventCount: 0, detailLoaded: false,
      blocks: [{
        kind: "text", message_id: "direct-answer-final", channel: "final",
        text: "你好。", done: true,
      }],
    }],
    engine: "codex", onEdit: () => {}, onGetDiff: () => {},
    onLoadDetail: () => {},
  }));
  assert.doesNotMatch(directAnswerMarkup, /已处理/,
    "a direct answer with no process remains free of a synthetic disclosure");

  const compactToolGapTurn: Turn = {
    id: "compact-tool-gap", clientMsgId: "compact-client",
    historyTurnId: "compact-history", prompt: "继续长任务", done: true,
    forkPointId: "compact-fork-point",
    ts: Date.UTC(2026, 7, 20, 9, 10),
    doneTs: Date.UTC(2026, 7, 20, 9, 11),
    blocks: [{
      kind: "process", item_id: "completed-before-compact",
      processKind: "command", phase: "end", status: "succeeded",
      title: "压缩前工具已完成", done: true,
    }, {
      kind: "text", message_id: "internal-compact-summary",
      channel: "final", text: "内部压缩摘要", done: true,
    }],
  };
  const compactToolGapMarkup = renderToStaticMarkup(createElement(ChatView, {
    sid: idleSid, turns: [compactToolGapTurn], engine: "codex",
    activeTurnId: compactToolGapTurn.id,
    onEdit: () => {}, onGetDiff: () => {}, onFork: () => {},
  }));
  assert.match(compactToolGapMarkup, /turn-process-label running status-shimmer is-active/,
    "an exact active task keeps its process disclosure running across a compact tool gap");
  assert.match(compactToolGapMarkup,
    /class="turn-working"[\s\S]*处理中/,
    "an internal final answer cannot settle the exact enclosing native task");
  assert.match(compactToolGapMarkup, /class="turn-process open"/,
    "running History refreshes do not repeatedly collapse the active process");
  assert.doesNotMatch(compactToolGapMarkup, /class="ubub-meta ai-meta"/,
    "a compact continuation cannot expose completion metadata while working");
  assert.doesNotMatch(compactToolGapMarkup, /aria-label="派生"/,
    "a compact continuation cannot expose fork while working");
  assert.equal(
    compactToolGapMarkup.match(/class="ubub-time"/g)?.length ?? 0,
    1,
    "the user prompt keeps its timestamp without an early completion time",
  );
  const unrelatedOwnerMarkup = renderToStaticMarkup(createElement(ChatView, {
    sid: idleSid, turns: [compactToolGapTurn], engine: "codex",
    activeTurnId: "another-turn",
    onEdit: () => {}, onGetDiff: () => {}, onFork: () => {},
  }));
  assert.doesNotMatch(unrelatedOwnerMarkup, /class="turn-working"/,
    "session activity cannot leak onto a turn which does not own it");
  assert.match(unrelatedOwnerMarkup, /turn-process-label done status-shimmer/,
    "removing the exact owner settles the process exactly once");
  assert.match(unrelatedOwnerMarkup, /class="ubub-meta ai-meta"/,
    "the real terminal reveals completion metadata");
  assert.match(unrelatedOwnerMarkup, /aria-label="派生"/,
    "the real terminal reveals fork");
  assert.equal(
    unrelatedOwnerMarkup.match(/class="ubub-time"/g)?.length ?? 0,
    2,
    "the real terminal adds one completion time beside the prompt time",
  );

  const staleChildTurn: Turn = {
    id: "steer-predecessor", prompt: "start", done: true,
    historyTurnId: "shared-native-task",
    blocks: [{
      kind: "process", item_id: "late-predecessor-child",
      processKind: "agent", phase: "update", status: "running",
      title: "旧分段后台任务", done: false,
    }],
  };
  const currentSteerTurn: Turn = {
    id: "current-steer", clientMsgId: "current-steer",
    historyTurnId: "shared-native-task", prompt: "继续", done: false,
    blocks: [],
  };
  const singleOwnerMarkup = renderToStaticMarkup(createElement(ChatView, {
    sid: idleSid, turns: [staleChildTurn, currentSteerTurn], engine: "codex",
    activeTurnId: currentSteerTurn.id,
    onEdit: () => {}, onGetDiff: () => {},
  }));
  assert.equal(
    singleOwnerMarkup.match(/class="turn-working"/g)?.length ?? 0,
    1,
    "one native task may render only one session-level working spark after steer",
  );
  assert.match(singleOwnerMarkup, /旧分段后台任务/,
    "suppressing the duplicate spark keeps the predecessor process visible");
  const ambiguousOwnerMarkup = renderToStaticMarkup(createElement(ChatView, {
    sid: idleSid, turns: [staleChildTurn, currentSteerTurn], engine: "codex",
    activeTurnId: null,
    ambiguousActiveTurnIds: [staleChildTurn.id, currentSteerTurn.id],
    onEdit: () => {}, onGetDiff: () => {},
  }));
  assert.equal(
    ambiguousOwnerMarkup.match(/class="turn-working"/g)?.length ?? 0,
    1,
    "an ambiguous shared native owner still renders at most one top-level spark",
  );
  const idleAmbiguousRowsMarkup = renderToStaticMarkup(createElement(ChatView, {
    sid: idleSid, turns: [staleChildTurn, currentSteerTurn], engine: "codex",
    activeTurnId: null,
    onEdit: () => {}, onGetDiff: () => {},
  }));
  assert.doesNotMatch(idleAmbiguousRowsMarkup, /class="turn-working"/,
    "idle or missing-owner state cannot revive a cached open row");

  const currentRunningState = reduce({
    ...initialState,
    focusedSid: idleSid,
    sessions: [{ session_id: idleSid, engine: "codex" as const }],
    runtimes: { [idleSid]: {
      ...createRuntime(), state: "running" as const,
      historyRevision: idleRevision, historyGeneration: idleGeneration,
      lastLiveSeq: 40, liveDetailTurnIds: [summaryTurn.id],
      turns: [openObserved()],
    } },
  }, { type: "event", event: event({
    ...idleHistory,
    in_progress: true,
  }) });
  assert.equal(currentRunningState.runtimes[idleSid].turns[0]
    .detailProjection?.blocks.find(
      (block: Block) => block.kind === "process")?.done, false,
  "a currently running History page preserves genuine Codex process activity");

  const racedLiveState = reduce({
    ...initialState,
    focusedSid: idleSid,
    sessions: [{ session_id: idleSid, engine: "codex" as const }],
    runtimes: { [idleSid]: {
      ...createRuntime(), state: "running" as const,
      historyRevision: idleRevision, historyGeneration: idleGeneration,
      lastLiveSeq: 41, liveDetailTurnIds: [summaryTurn.id],
      turns: [openObserved()],
    } },
  }, { type: "event", event: idleHistory });
  const racedTurn = racedLiveState.runtimes[idleSid].turns[0];
  const racedProcess = [
    ...(racedTurn.detailProjection?.blocks ?? []),
    ...(racedTurn.liveSpillBlocks ?? []),
    ...racedTurn.blocks,
  ].find((block: Block) => block.kind === "process");
  assert.equal(racedProcess?.done, false,
    "an older idle History page cannot settle a newer live process event");

  const lostTerminalSid = "codex-idle-history-recovery-source";
  const lostTerminalState = reduce({
    ...initialState,
    focusedSid: lostTerminalSid,
    sessions: [{ session_id: lostTerminalSid, engine: "codex" as const }],
    runtimes: { [lostTerminalSid]: {
      ...createRuntime(), state: "running" as const,
      turns: [{
        id: "promptless-wrapper-generation-tail", prompt: "", done: false,
        blocks: [{
          kind: "tool" as const,
          message_id: "promptless-wrapper-generation-envelope",
          tool_use_id: "promptless-wrapper-generation-tool",
          tool: "shell", input: {}, done: true,
          result: { content: "done", is_error: false },
        }],
      }],
    } },
  }, { type: "event", event: event({
    type: "history", sid: lostTerminalSid, session_id: lostTerminalSid,
    revision: "lost-terminal-r1", generation: "lost-terminal-g1",
    build_seq: 1, live_seq: 0, authoritative: true,
    detail: "summary", in_progress: false, has_more: false,
    events: [], turns: [],
  }) });
  assert.deepEqual(
    {
      done: lostTerminalState.runtimes[lostTerminalSid].turns[0].done,
      terminalSource:
        lostTerminalState.runtimes[lostTerminalSid].turns[0].terminalSource,
      error: lostTerminalState.runtimes[lostTerminalSid].turns[0].error,
    },
    {
      done: true,
      terminalSource: "idle_history_recovery",
      error: "会话已结束，但未收到完整的终止状态。",
    },
    "a new idle-History fallback carries a machine-readable recovery source",
  );

  const cachedInput = openObserved();
  const cachedState = reduce({
    ...initialState,
    focusedSid: idleSid,
    sessions: [{ session_id: idleSid, engine: "codex" as const }],
    runtimes: { [idleSid]: createRuntime() },
  }, {
    type: "hydrate_cache", sid: idleSid, turns: [cachedInput],
    revision: idleRevision, generation: idleGeneration,
  });
  assert.equal(cachedState.runtimes[idleSid].turns[0].blocks[0].done, true,
    "an IndexedDB paint cannot animate stale completed child activity");
  assert.equal(cachedInput.blocks[0].done, false,
    "cache hydration cannot mutate its action payload");

  const delayedPlanCache: Turn = {
    id: summaryTurn.id, prompt: summaryTurn.prompt, done: true,
    blocks: [{
      kind: "process", item_id: "delayed-cached-plan",
      processKind: "plan", phase: "update", status: "running",
      title: "计划", plan: [{ step: "done", status: "inProgress" }],
      done: false,
    }],
  };
  const historyBeforeCache = reduce({
    ...initialState,
    focusedSid: idleSid,
    sessions: [{ session_id: idleSid, engine: "codex" as const }],
    runtimes: { [idleSid]: {
      ...createRuntime(), state: "running" as const,
      liveOwner: { turnId: summaryTurn.id, seq: 39 },
      turns: [summaryTurn],
    } },
  }, { type: "event", event: idleHistory });
  assert.equal(historyBeforeCache.runtimes[idleSid].liveOwner, null,
    "an authoritative idle History page retires its completed owner");
  const delayedIdleCacheState = reduce(historyBeforeCache, {
    type: "hydrate_cache", sid: idleSid, turns: [delayedPlanCache],
    revision: idleRevision, generation: idleGeneration,
  });
  const delayedIdlePlan = delayedIdleCacheState.runtimes[idleSid].turns[0]
    .detailProjection?.blocks.find((block: Block) =>
      block.kind === "process" && block.processKind === "plan");
  assert.equal(delayedIdleCacheState.runtimes[idleSid].state, "idle");
  assert.equal(delayedIdlePlan?.done, true,
    "History(idle) followed by same-scope cache hydration stays settled");

  let nextTaskBeforeBinding = reduce(historyBeforeCache, {
    type: "query_sent", sid: idleSid, prompt: "next task",
    msg_id: "next-task-client-id", ts: 50_000,
  });
  nextTaskBeforeBinding = reduce(nextTaskBeforeBinding, {
    type: "event", event: event({
      type: "state", sid: idleSid, state: "running", seq: 41,
    }),
  });
  assert.equal(nextTaskBeforeBinding.runtimes[idleSid].liveOwner, null,
    "the next running state cannot reclaim the completed owner before binding");
  const nextTaskAfterDelayedCache = reduce(nextTaskBeforeBinding, {
    type: "hydrate_cache", sid: idleSid, turns: [delayedPlanCache],
    revision: idleRevision, generation: idleGeneration,
  });
  const predecessorPlan = nextTaskAfterDelayedCache.runtimes[idleSid]
    .turns[0].detailProjection?.blocks.find((block: Block) =>
      block.kind === "process" && block.processKind === "plan");
  assert.equal(predecessorPlan?.done, true,
    "task B cannot revive task A's cached Plan before its own binding");

  const cacheAfterAuthority = (
    lastLiveSeq: number,
    lastLifecycleSeq: number,
  ) => reduce({
    ...historyBeforeCache,
    runtimes: { [idleSid]: {
      ...historyBeforeCache.runtimes[idleSid],
      state: "idle" as const,
      lastLiveSeq,
      lastLifecycleSeq,
      liveOwner: lastLiveSeq > lastLifecycleSeq
        ? { turnId: summaryTurn.id, seq: lastLiveSeq }
        : null,
    } },
  }, {
    type: "hydrate_cache", sid: idleSid, turns: [delayedPlanCache],
    revision: idleRevision, generation: idleGeneration,
  }).runtimes[idleSid].turns[0].detailProjection?.blocks.find(
    (block: Block) => block.kind === "process"
      && block.processKind === "plan");
  assert.equal(cacheAfterAuthority(41, 40)?.done, false,
    "a live frame newer than History provisionally preserves its open Plan");
  assert.equal(cacheAfterAuthority(41, 42)?.done, true,
    "a lifecycle terminal newer than that live frame settles delayed cache");

  const idleBoundaryState = reduce({
    ...initialState,
    focusedSid: idleSid,
    sessions: [{ session_id: idleSid, engine: "codex" as const }],
    runtimes: { [idleSid]: {
      ...createRuntime(), state: "running" as const,
      liveOwner: { turnId: summaryTurn.id, seq: 41 },
      turns: [openObserved()],
    } },
  }, { type: "event", event: event({
    type: "state", sid: idleSid, state: "idle", seq: 42,
  }) });
  assert.equal(idleBoundaryState.runtimes[idleSid].turns[0].blocks[0].done,
    true, "an exact Codex idle frame settles stale display-only activity");
  assert.equal(idleBoundaryState.runtimes[idleSid].turns[0].blocks[0].kind
    === "process"
    ? idleBoundaryState.runtimes[idleSid].turns[0].blocks[0].output : null,
  "keep this output", "the idle boundary preserves process payloads");
  assert.equal(idleBoundaryState.runtimes[idleSid].liveOwner, null,
    "State(idle) retires the completed turn's runtime owner");

  const resumedBackgroundState = reduce(idleBoundaryState, {
    type: "event", event: event({
      type: "process", sid: idleSid, seq: 43,
      item_id: "shared-cli-replayed-process", kind: "command",
      phase: "update", status: "running", turn_id: summaryTurn.id,
      title: "real background work resumed",
    }),
  });
  assert.equal(resumedBackgroundState.runtimes[idleSid].turns[0]
    .blocks[0].done, false,
  "a later real process event can reopen its exact settled child");

  const runningBoundaryState = reduce({
    ...initialState,
    focusedSid: idleSid,
    sessions: [{ session_id: idleSid, engine: "codex" as const }],
    runtimes: { [idleSid]: {
      ...createRuntime(), state: "idle" as const,
      turns: [openObserved()],
    } },
  }, { type: "event", event: event({
    type: "state", sid: idleSid, state: "running", seq: 42,
  }) });
  assert.equal(runningBoundaryState.runtimes[idleSid].turns[0].blocks[0].done,
    false, "a running frame cannot settle genuine Codex process activity");

  const claudeSid = "claude-background-after-terminal-history";
  let claudeState = {
    ...initialState,
    focusedSid: claudeSid,
    sessions: [{ session_id: claudeSid, engine: "claude" as const }],
    runtimes: { [claudeSid]: {
      ...createRuntime(), state: "idle" as const,
      historyRevision: "claude-background-r1",
      historyGeneration: "claude-background-g1", lastLiveSeq: 50,
      liveDetailTurnIds: ["claude-background-row"],
      turns: [{ id: "claude-background-row", prompt: "delegate", done: true,
        blocks: [{ kind: "process" as const,
          item_id: "claude-background-agent", processKind: "agent" as const,
          phase: "start" as const, status: "running" as const,
          title: "background", background: true, done: false }] }],
    } },
  };
  claudeState = reduce(claudeState, { type: "event", event: event({
    type: "history", sid: claudeSid, session_id: claudeSid,
    revision: "claude-background-r1", generation: "claude-background-g1",
    build_seq: 2, live_seq: 50, authoritative: true, detail: "summary",
    in_progress: false, has_more: false, newest_id: "claude-background-row",
    events: [], turns: [{ id: "claude-background-row", prompt: "delegate",
      done: true, detailEventCount: 1, detailLoaded: false, blocks: [] }],
  }) });
  assert.equal(claudeState.runtimes[claudeSid].turns[0]
    .detailProjection?.blocks[0]?.done, false,
  "Claude terminal repair cannot close an explicitly detached background process");
} finally {
  await reducerHarness.close();
}
