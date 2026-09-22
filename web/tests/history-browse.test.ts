import assert from "node:assert/strict";

import {
  appendNewerPage,
  cachedLatestRequiresLiveRuntime,
  canonicalTurnId,
  createHistoryBrowse,
  markBrowseDetail,
  markBrowseDetailLoading,
  markBrowseLatestDirty,
  markBrowseNewerUnavailable,
  settleBrowsePageRequest,
  prependOlderPage,
  type HistoryBrowseLimits,
  type HistoryBrowsePage,
} from "../src/history-browse.ts";
import {
  installAuthoritativeTurnDetailPage,
  mergeInitialHistory,
} from "../src/history-merge.ts";
import { protectedHistoryTurnIds } from "../src/history-selection-guard.ts";
import {
  nextActiveDetailRequest,
  installTurnDetailProjectionPage,
  nextAutoLoadDetailTurn,
} from "../src/history-detail-projection.ts";
import type { ProcessEvent, ServerEvent } from "../src/protocol.ts";
import type { Turn } from "../src/reducer.ts";

function turn(
  id: string,
  options: Partial<Turn> = {},
): Turn {
  return {
    id,
    prompt: `prompt-${id}`,
    blocks: [],
    done: true,
    ts: Number(id.replace(/\D/g, "")) || undefined,
    ...options,
  };
}

function limits(maxTurns: number, lowWaterTurns = maxTurns): HistoryBrowseLimits {
  return {
    maxTurns,
    lowWaterTurns,
    maxBytes: Number.MAX_SAFE_INTEGER,
    lowWaterBytes: Number.MAX_SAFE_INTEGER,
  };
}

const scopeKey = "machine-a\u0000code\u0000codex";
const selectionGuard = {
  sid: "session-a",
  revision: "revision-a",
  viewId: "view-a",
  scopeKey,
  turnIds: ["turn-2", "turn-8"] as [string, string],
};
assert.deepEqual(protectedHistoryTurnIds(
  "turn-5",
  selectionGuard,
  {
    sid: "session-a",
    revision: "revision-a",
    viewId: "view-a",
    scopeKey,
  },
), ["turn-5", "turn-2", "turn-8"]);
for (const mismatched of [
  { ...selectionGuard, sid: "session-b" },
  { ...selectionGuard, revision: "revision-b" },
  { ...selectionGuard, viewId: "view-b" },
  { ...selectionGuard, scopeKey: "another-machine" },
]) {
  assert.deepEqual(protectedHistoryTurnIds(
    "turn-5",
    mismatched,
    {
      sid: "session-a",
      revision: "revision-a",
      viewId: "view-a",
      scopeKey,
    },
  ), ["turn-5"], "a stale text selection cannot protect another history view");
}

assert.equal(canonicalTurnId(turn("optimistic", {
  historyTurnId: "native-user-message",
})), "native-user-message");
assert.equal(canonicalTurnId(turn("plain")), "plain");

const sharedDisplayIdOlder = turn("shared-display", {
  historyTurnId: "canonical-older",
  prompt: "older legitimate row",
});
const sharedDisplayIdNewer = turn("shared-display", {
  historyTurnId: "canonical-newer",
  prompt: "newer legitimate row",
});
const compactionNativeId = "cross-page-compaction-native";
const compactionOwner = turn("compaction-owner", {
  forkPointId: compactionNativeId,
  prompt: "canonical compaction owner",
  blocks: [{
    kind: "process",
    item_id: "canonical-compaction",
    processKind: "compaction",
    phase: "end",
    status: "succeeded",
    turn_id: compactionNativeId,
    title: "压缩上下文",
    done: true,
  }],
});
const compactionOrphan = turn("compaction-orphan", {
  prompt: "",
  blocks: [{
    kind: "process",
    item_id: "stale-compaction",
    processKind: "compaction",
    phase: "end",
    status: "succeeded",
    turn_id: compactionNativeId,
    title: "压缩上下文",
    done: true,
  }],
});
const displayAliasRepair = createHistoryBrowse({
  scopeKey,
  sid: "display-alias-repair",
  revision: "display-alias-revision",
  generation: "display-alias-generation",
  viewId: "display-alias-view",
  baseTurns: [sharedDisplayIdNewer, compactionOwner],
  basePageKey: "display-alias-head",
  hasOlder: true,
  olderCursor: "display-alias-cursor",
  olderPage: {
    pageKey: "display-alias-older",
    turns: [sharedDisplayIdOlder, compactionOrphan],
    hasOlder: false,
    olderCursor: null,
    newerPageKey: "display-alias-head",
  },
  limits: limits(10),
});
assert.deepEqual(
  displayAliasRepair.projection.turns.map((item) => [
    item.id, item.historyTurnId, item.prompt,
  ]),
  [
    ["shared-display", "canonical-older", "older legitimate row"],
    ["compaction-orphan", undefined, ""],
    ["shared-display", "canonical-newer", "newer legitimate row"],
    ["compaction-owner", undefined, "canonical compaction owner"],
  ],
  "cross-page reconciliation preserves display collisions and distinct compactions",
);
assert.deepEqual(nextAutoLoadDetailTurn([
  turn("complete", {
    detailAutoLoad: true,
    detailHasMore: false,
    detailOldestCursor: "ignored",
  }),
  turn("loading", {
    detailAutoLoad: true,
    detailLoading: true,
    detailHasMore: true,
    detailOldestCursor: "also-ignored",
  }),
  turn("next", {
    detailAutoLoad: true,
    detailHasMore: true,
    detailOldestCursor: "detail-before",
  }),
]), { turnId: "next", before: "detail-before" },
"automatic detail paging selects exactly one idle cursor-backed turn");
assert.equal(nextAutoLoadDetailTurn([
  turn("manual", {
    detailAutoLoad: false,
    detailHasMore: true,
    detailOldestCursor: "manual-only",
  }),
]), null, "manual/cancelled detail cannot restart automatic paging");

const activeDetail = turn("active-native", {
  clientMsgId: "active-browser",
  done: false,
  detailLoaded: false,
});
assert.deepEqual(nextActiveDetailRequest(
  [turn("older-open", { done: false }), activeDetail],
  "active-browser",
  true,
), { turnId: "active-native" },
"the exact authoritative unfinished head requests its initial detail page");
assert.equal(nextActiveDetailRequest(
  [activeDetail, { ...activeDetail, id: "duplicate-active" }],
  "active-browser",
  true,
), null, "ambiguous aliases cannot trigger an authoritative detail read");
assert.equal(nextActiveDetailRequest(
  [{
    ...activeDetail,
    detailProjection: {
      segments: [{
        pageKey: "latest", before: null, events: [], hasMore: false,
        oldestCursor: null, hasNewer: false, newerCursor: null,
        encodedChars: 0,
      }],
      blocks: [], capped: false, hasMore: false, oldestCursor: null,
      hasNewer: false, newerCursor: null,
    },
  }],
  "active-browser",
  true,
), null, "an installed canonical page suppresses duplicate initial loading");
assert.equal(nextActiveDetailRequest(
  [{
    ...activeDetail,
    detailAutoLoad: false,
    detailProjection: {
      segments: [{
        pageKey: "latest", before: null, events: [], hasMore: true,
        oldestCursor: "active-detail-before", hasNewer: false,
        newerCursor: null, encodedChars: 0,
      }],
      blocks: [], capped: false, hasMore: true,
      oldestCursor: "active-detail-before", hasNewer: false,
      newerCursor: null,
    },
  }],
  "active-browser",
  true,
), null,
"an installed active detail page leaves older pages to explicit pagination");
assert.deepEqual(nextActiveDetailRequest(
  [{
    ...activeDetail,
    detailHasMore: true,
    detailOldestCursor: "stale-active-detail-before",
  }],
  "active-browser",
  true,
), { turnId: "active-native" },
"active recovery starts from the bounded newest page without a projection");
assert.equal(nextActiveDetailRequest(
  [{
    ...activeDetail,
    detailProjection: {
      segments: [{
        pageKey: "latest", before: null, events: [], hasMore: true,
        oldestCursor: "capped-before", hasNewer: false, newerCursor: null,
        encodedChars: 0,
      }],
      blocks: [], capped: true, hasMore: true,
      oldestCursor: "capped-before", hasNewer: false, newerCursor: null,
    },
  }],
  "active-browser",
  true,
), null, "a capped active projection cannot start an automatic paging loop");
assert.equal(nextActiveDetailRequest(
  [activeDetail], "active-browser", false,
), null, "an idle runtime cannot infer activity from an open-looking row");

const olderPage: HistoryBrowsePage = {
  pageKey: "page-older",
  turns: [turn("1"), turn("2")],
  hasOlder: true,
  olderCursor: "1",
  newerPageKey: "page-latest",
};
const initial = createHistoryBrowse({
  scopeKey,
  sid: "session-a",
  revision: "revision-a",
  generation: "generation-a",
  viewId: "view-a",
  baseTurns: [turn("3"), turn("4")],
  basePageKey: "page-latest",
  hasOlder: true,
  olderCursor: "3",
  olderPage,
  limits: limits(10),
});
assert.deepEqual(initial.projection.turns.map((item) => item.id), [
  "1", "2", "3", "4",
]);
assert.deepEqual(initial.projection.loadedPageKeys, [
  "page-older", "page-latest",
]);
assert.equal(initial.projection.scopeKey, scopeKey);
assert.equal(initial.projection.viewId, "view-a");
assert.equal(initial.projection.windowEpoch, 1);
assert.equal(initial.projection.hasOlder, true);
assert.equal(initial.projection.olderCursor, "1");
assert.equal(initial.projection.hasNewer, false);
assert.deepEqual(initial.evictedPages, []);

// The newer runtime identity wins an overlap with a native history page, while
// the canonical id still deduplicates both rows.
const overlap = createHistoryBrowse({
  scopeKey,
  sid: "session-overlap",
  revision: "revision-overlap",
  viewId: "view-overlap",
  baseTurns: [turn("optimistic-2", {
    historyTurnId: "native-2",
    prompt: "same",
  }), turn("3")],
  basePageKey: "page-latest",
  hasOlder: true,
  olderCursor: "native-2",
  olderPage: {
    pageKey: "page-older",
    turns: [turn("1"), turn("native-2", { prompt: "same" })],
    hasOlder: false,
    olderCursor: "1",
    newerPageKey: "page-latest",
  },
  limits: limits(10),
}).projection;
assert.deepEqual(overlap.turns.map((item) => item.id), [
  "1", "optimistic-2", "3",
]);

// Claude live rows use browser ids; native summary pages carry the mapping in
// clientMsgId. Only the first greeting and the newest four rows have already
// been reconciled when an older page overlaps the still-resident live turns.
const claudeHistory = Array.from({ length: 10 }, (_, index) => turn(
  `claude-native-${index + 1}`, {
    clientMsgId: `claude-browser-${index + 1}`,
    prompt: index === 0 ? "hi claude" : `Claude question ${index + 1}`,
    ts: 1_000 + index * 1_000,
    blocks: [{
      kind: "text", message_id: `claude-answer-${index + 1}`,
      channel: "final", text: `answer ${index + 1}`, done: true,
    }],
  },
));
const claudeLive = claudeHistory.map((item, index) =>
  index === 0 || index >= 6 ? item : {
    ...item, id: item.clientMsgId!, clientMsgId: undefined,
  });
claudeLive[1] = {
  ...claudeLive[1],
  detailLoaded: true,
  blocks: [{
    kind: "tool", tool_use_id: "claude-tool", message_id: "claude-tool-message",
    tool: "Read", input: { path: "example.txt" }, done: true,
  }, ...claudeLive[1].blocks],
};
const claudeOlderPage: HistoryBrowsePage = {
  pageKey: "claude-older", turns: claudeHistory.slice(0, 6),
  hasOlder: false, olderCursor: claudeHistory[0].id,
  newerPageKey: "claude-latest",
};
const claudeOverlap = createHistoryBrowse({
  scopeKey: "machine-a\u0000code\u0000claude",
  sid: "claude-session", revision: "claude-revision", viewId: "claude-view",
  baseTurns: claudeLive, basePageKey: "claude-latest", hasOlder: true,
  olderCursor: claudeHistory[6].id, olderPage: claudeOlderPage,
  limits: limits(20),
}).projection;
assert.deepEqual(claudeOverlap.turns.map((item) => item.prompt),
  claudeHistory.map((item) => item.prompt),
  "overlapping Claude pages keep ten unique turns with the greeting first");
assert.deepEqual(claudeOverlap.turns.map((item) => item.id),
  claudeLive.map((item) => item.id), "existing display ids stay mounted");
assert.equal(claudeOverlap.turns[1].historyTurnId, claudeHistory[1].id,
  "the merged row retains its native detail lookup id");
assert.equal(claudeOverlap.turns[1].blocks.filter((block) =>
  block.kind === "tool" && block.tool_use_id === "claude-tool").length, 1,
  "already expanded process detail survives the summary overlap");
assert.deepEqual(prependOlderPage(claudeOverlap, claudeOlderPage).projection.turns,
  claudeOverlap.turns, "loading the same older page again is idempotent");

const ambiguousBrowserRows = createHistoryBrowse({
  scopeKey, sid: "ambiguous-browser", revision: "r", viewId: "v",
  baseTurns: [turn("shared-browser", { prompt: "repeat", ts: 3 })],
  basePageKey: "latest", hasOlder: true, olderCursor: "cursor",
  olderPage: {
    pageKey: "older", turns: [
      turn("native-a", { clientMsgId: "shared-browser", prompt: "repeat", ts: 1 }),
      turn("native-b", { clientMsgId: "shared-browser", prompt: "repeat", ts: 2 }),
    ], hasOlder: false,
  },
}).projection;
assert.equal(ambiguousBrowserRows.turns.length, 3,
  "ambiguous client aliases and repeated prompts never erase distinct turns");

const claudeRunning = createHistoryBrowse({
  scopeKey, sid: "claude-running", revision: "r", viewId: "v",
  baseTurns: [{ ...claudeLive[1], done: false, doneTs: undefined }],
  basePageKey: "latest", hasOlder: true, olderCursor: "cursor",
  olderPage: {
    pageKey: "older", turns: [{ ...claudeHistory[1], interrupted: true, error: "old terminal" }],
    hasOlder: false,
  },
}).projection;
assert.equal(claudeRunning.turns.length, 1);
assert.equal(claudeRunning.turns[0].done, false);
assert.equal(claudeRunning.turns[0].interrupted, undefined);
assert.equal(claudeRunning.turns[0].error, undefined);
assert.equal(claudeRunning.turns[0].doneTs, undefined,
  "an older browse page never supplies a terminal to the newer open copy");

// Loading older rows evicts completed rows from the newer edge down to the low
// water mark and returns the complete pre-eviction segment for IndexedDB.
const tailEviction = createHistoryBrowse({
  scopeKey,
  sid: "session-tail",
  revision: "revision-tail",
  viewId: "view-tail",
  baseTurns: [turn("4"), turn("5")],
  basePageKey: "page-latest",
  hasOlder: true,
  olderCursor: "4",
  olderPage: {
    pageKey: "page-older",
    turns: [turn("1"), turn("2"), turn("3")],
    hasOlder: false,
    olderCursor: "1",
    newerPageKey: "page-latest",
  },
  limits: limits(4, 3),
});
assert.deepEqual(tailEviction.projection.turns.map((item) => item.id), [
  "1", "2", "3",
]);
assert.equal(tailEviction.projection.hasNewer, true);
assert.equal(tailEviction.projection.newerPageKey, "page-latest");
assert.equal(tailEviction.evictedPages.length, 1);
assert.equal(tailEviction.evictedPages[0].pageKey, "page-latest");
assert.deepEqual(tailEviction.evictedPages[0].turns.map((item) => item.id), [
  "4", "5",
]);

// A local newer page reverses the window. Its head eviction exposes a valid
// server-before cursor at the oldest retained row.
const headEviction = appendNewerPage(tailEviction.projection, {
  pageKey: "page-latest",
  turns: [turn("4"), turn("5")],
  isLatest: true,
  hasNewer: false,
  newerPageKey: null,
}, {
  expectedScopeKey: scopeKey,
  limits: limits(4, 3),
});
assert.deepEqual(headEviction.projection.turns.map((item) => item.id), [
  "3", "4", "5",
]);
assert.equal(headEviction.projection.hasOlder, true);
assert.equal(headEviction.projection.olderCursor, "3");
assert.equal(headEviction.projection.hasNewer, false);
assert.equal(headEviction.projection.newerPageKey, null);
assert.equal(headEviction.evictedPages[0].pageKey, "page-older");
assert.deepEqual(headEviction.evictedPages[0].turns.map((item) => item.id), [
  "1", "2", "3",
]);

// A viewport anchor is a hard eviction boundary. Going past it would create a
// hole around the row whose pixel offset ChatView is preserving.
const anchoredBase = createHistoryBrowse({
  scopeKey,
  sid: "session-anchor",
  revision: "revision-anchor",
  viewId: "view-anchor",
  baseTurns: [turn("3"), turn("4")],
  basePageKey: "page-latest",
  hasOlder: true,
  olderCursor: "3",
  limits: limits(3, 2),
}).projection;
const anchored = prependOlderPage(anchoredBase, {
  pageKey: "page-older",
  turns: [turn("1"), turn("2")],
  hasOlder: false,
  olderCursor: "1",
  newerPageKey: "page-latest",
}, {
  expectedScopeKey: scopeKey,
  protectedTurnIds: ["4"],
  limits: limits(3, 2),
});
assert.deepEqual(anchored.projection.turns.map((item) => item.id), [
  "1", "2", "3", "4",
]);
assert.deepEqual(anchored.evictedPages, []);

// The browse projection is display-only: an active runtime tail must not act
// as an implicit anchor and let each older page grow the window forever.
const active = turn("30", {
  done: false,
  blocks: [{
    kind: "process",
    item_id: "process-30",
    processKind: "command",
    phase: "start",
    status: "running",
    title: "running",
    done: false,
  }],
});
const activeFirstPage = createHistoryBrowse({
  scopeKey,
  sid: "session-active",
  revision: "revision-active",
  viewId: "view-active",
  baseTurns: [
    ...Array.from({ length: 9 }, (_, index) => turn(String(index + 21))),
    active,
  ],
  basePageKey: "page-latest",
  hasOlder: true,
  olderCursor: "21",
  olderPage: {
    pageKey: "page-11-20",
    turns: Array.from({ length: 10 }, (_, index) => turn(String(index + 11))),
    hasOlder: true,
    olderCursor: "11",
    newerPageKey: "page-latest",
  },
  limits: limits(20, 16),
});
assert.equal(activeFirstPage.projection.turns.length, 20);
assert.equal(activeFirstPage.projection.turns.at(-1)?.id, "30");
const activeBounded = prependOlderPage(activeFirstPage.projection, {
  pageKey: "page-1-10",
  turns: Array.from({ length: 10 }, (_, index) => turn(String(index + 1))),
  hasOlder: false,
  olderCursor: "1",
  newerPageKey: "page-11-20",
}, {
  expectedScopeKey: scopeKey,
  expectedOlderCursor: "11",
  limits: limits(20, 16),
});
assert.deepEqual(activeBounded.projection.turns.map((item) => item.id),
  Array.from({ length: 16 }, (_, index) => String(index + 1)));
assert.ok(activeBounded.projection.turns.length <= 20);
assert.equal(activeBounded.projection.turns.some((item) => item.id === "30"),
  false, "the authoritative runtime, not browse, owns the active tail");
assert.equal(activeBounded.projection.hasNewer, true);
assert.equal(activeBounded.projection.newerPageKey, "page-11-20");

// The same rule applies to the byte bound: open process output may be dropped
// from browse while it remains authoritative in SessionRuntime.
const byteBounded = createHistoryBrowse({
  scopeKey,
  sid: "session-active-bytes",
  revision: "revision-active-bytes",
  viewId: "view-active-bytes",
  baseTurns: [turn("3"), turn("4", {
    done: false,
    prompt: "x".repeat(4096),
    blocks: [{
      kind: "process",
      item_id: "process-4",
      processKind: "command",
      phase: "update",
      status: "running",
      title: "running",
      output: "y".repeat(4096),
      done: false,
    }],
  })],
  basePageKey: "page-latest",
  hasOlder: true,
  olderCursor: "3",
  olderPage: {
    pageKey: "page-older",
    turns: [turn("1"), turn("2")],
    hasOlder: false,
    olderCursor: "1",
    newerPageKey: "page-latest",
  },
  limits: {
    maxTurns: 100,
    lowWaterTurns: 100,
    maxBytes: 2048,
    lowWaterBytes: 1024,
  },
});
assert.ok(new TextEncoder().encode(
  JSON.stringify(byteBounded.projection.turns),
).byteLength <= 2048);
assert.equal(byteBounded.projection.turns.some((item) => item.id === "4"), false);

// detailLoading is also not an implicit page anchor. A frozen viewport anchor
// remains the sole reason browse may intentionally overflow its bound.
const detailLoadingBase = {
  scopeKey,
  sid: "session-detail-boundary",
  revision: "revision-detail-boundary",
  viewId: "view-detail-boundary",
  baseTurns: [turn("3"), turn("4", { detailLoading: true })],
  basePageKey: "page-latest",
  hasOlder: true,
  olderCursor: "3",
  olderPage: {
    pageKey: "page-older",
    turns: [turn("1"), turn("2")],
    hasOlder: false,
    olderCursor: "1",
    newerPageKey: "page-latest",
  },
  limits: limits(3, 2),
};
const unanchoredDetail = createHistoryBrowse(detailLoadingBase);
assert.deepEqual(unanchoredDetail.projection.turns.map((item) => item.id),
  ["1", "2"]);
const anchoredDetail = createHistoryBrowse({
  ...detailLoadingBase,
  protectedTurnIds: ["4"],
});
assert.deepEqual(anchoredDetail.projection.turns.map((item) => item.id),
  ["1", "2", "3", "4"]);
assert.deepEqual(anchoredDetail.evictedPages, []);

// Frozen request scope is an authorization/display boundary. A late page from
// another machine/surface leaves this projection byte-for-byte unchanged.
const staleScope = prependOlderPage(initial.projection, {
  pageKey: "wrong-scope-page",
  turns: [turn("0")],
  hasOlder: false,
  olderCursor: "0",
}, {
  expectedScopeKey: "machine-b\u0000work\u0000claude",
  limits: limits(10),
});
assert.equal(staleScope.projection, initial.projection);
assert.deepEqual(staleScope.evictedPages, []);
assert.equal(prependOlderPage(initial.projection, {
  pageKey: "stale-epoch-page",
  turns: [turn("0")],
  hasOlder: false,
  olderCursor: "0",
}, {
  expectedScopeKey: scopeKey,
  expectedViewId: initial.projection.viewId,
  expectedWindowEpoch: initial.projection.windowEpoch - 1,
  expectedOlderCursor: initial.projection.olderCursor,
  limits: limits(10),
}).projection, initial.projection);

const detailed = markBrowseDetail(overlap, "native-2", turn("native-2", {
  prompt: "same",
  blocks: [{
    kind: "text",
    message_id: "answer-2",
    channel: "final",
    text: "full answer",
    done: true,
  }],
  detailLoaded: true,
}), undefined, {
  expectedScopeKey: scopeKey,
});
assert.equal(detailed.windowEpoch, overlap.windowEpoch,
  "detail hydration changes row height, not the page-window epoch");
assert.equal(detailed.turns[1].id, "optimistic-2");
assert.equal(detailed.turns[1].detailLoaded, true);
assert.equal(detailed.turns[1].blocks[0].kind, "text");
assert.equal(
  detailed.turns[1].blocks[0].kind === "text"
    ? detailed.turns[1].blocks[0].text
    : "",
  "full answer",
);

const finalPage = (
  summaryIds: string[],
  pageIds: string[],
): Turn => installAuthoritativeTurnDetailPage(
  turn("final-summary", {
    blocks: summaryIds.map((messageId) => ({
      kind: "text" as const,
      message_id: messageId,
      channel: "final" as const,
      text: `summary-${messageId}`,
      done: true,
    })),
  }),
  turn("final-detail", {
    blocks: pageIds.map((messageId) => ({
      kind: "text" as const,
      message_id: messageId,
      channel: "final" as const,
      text: `detail-${messageId}`,
      done: true,
    })),
  }),
  {
    hasMore: true,
    oldestCursor: "older-final-page",
    hasNewer: false,
    newerCursor: null,
  },
);
const finalIds = (value: Turn): string[] => value.blocks.flatMap((block) =>
  block.kind === "text" && block.channel === "final"
    ? [block.message_id] : []);
assert.deepEqual(finalIds(finalPage(["A", "B"], ["B"])), ["A", "B"],
  "an exact page final replaces B without deleting the unvisited summary A");
assert.deepEqual(finalIds(finalPage(["A"], ["C"])), ["C"],
  "a regenerated one-to-one final id replaces its summary alias");
assert.deepEqual(finalIds(finalPage(["A", "B"], ["B", "C"])), ["A", "B", "C"],
  "a page-only final after an exact anchor is appended without replacing A");

const olderDetailPage = markBrowseDetail(
  detailed,
  "native-2",
  turn("native-2", {
    prompt: "same",
    blocks: [{
      kind: "process",
      item_id: "older-detail-process",
      processKind: "command",
      phase: "end",
      status: "succeeded",
      title: "older detail page",
      done: true,
    }],
    detailLoaded: true,
  }),
  {
    hasMore: true,
    oldestCursor: "detail-cursor-oldest",
    hasNewer: true,
    newerCursor: "detail-cursor-newer",
  },
  { expectedScopeKey: scopeKey },
);
assert.deepEqual(
  olderDetailPage.turns[1].blocks.map((block) => block.kind),
  ["process", "text"],
  "an older intra-turn page replaces the process window but retains the summary final",
);
assert.equal(olderDetailPage.turns[1].detailHasMore, true);
assert.equal(
  olderDetailPage.turns[1].detailOldestCursor,
  "detail-cursor-oldest",
);
assert.equal(olderDetailPage.turns[1].detailHasNewer, true);
assert.equal(
  olderDetailPage.turns[1].detailNewerCursor,
  "detail-cursor-newer",
);

const newestDetailPage = markBrowseDetail(
  olderDetailPage,
  "native-2",
  turn("native-2", {
    prompt: "same",
    blocks: [{
      kind: "text",
      message_id: "newest-answer-2",
      channel: "final",
      text: "newest full answer",
      done: true,
    }],
    detailLoaded: true,
  }),
  {
    hasMore: true,
    oldestCursor: "detail-cursor-middle",
    hasNewer: false,
    newerCursor: null,
  },
  { expectedScopeKey: scopeKey },
);
assert.deepEqual(
  newestDetailPage.turns[1].blocks.map((block) => block.kind),
  ["text"],
  "moving to a newer intra-turn page replaces the prior process page",
);
assert.equal(
  newestDetailPage.turns[1].blocks[0].kind === "text"
    ? newestDetailPage.turns[1].blocks[0].text
    : "",
  "newest full answer",
);
assert.equal(newestDetailPage.turns[1].detailHasNewer, false);
assert.equal(newestDetailPage.turns[1].detailNewerCursor, null);

function processEvent(itemId: string, order: number): ProcessEvent {
  return {
    v: 22,
    type: "process",
    ts: order,
    sid: "session-a",
    item_id: itemId,
    kind: "command",
    phase: "end",
    status: "succeeded",
    title: itemId,
  };
}

function decodeDetailEvents(events: ServerEvent[]): Turn {
  const blocks: Turn["blocks"] = [];
  const indexes = new Map<string, number>();
  for (const event of events) {
    if (event.type !== "process") continue;
    const existing = indexes.get(event.item_id);
    const block = {
      kind: "process" as const,
      item_id: event.item_id,
      processKind: event.kind,
      phase: event.phase,
      status: event.status,
      title: event.title,
      output: event.output ?? undefined,
      done: event.phase === "end",
    };
    if (existing == null) {
      indexes.set(event.item_id, blocks.length);
      blocks.push(block);
    } else {
      blocks[existing] = { ...blocks[existing], ...block };
    }
  }
  return turn("detail-projection", { blocks });
}

let cumulative = installTurnDetailProjectionPage(undefined, {
  events: [
    processEvent("newer-1", 30),
    processEvent("newer-2", 31),
  ],
  hasMore: true,
  oldestCursor: "cursor-middle",
  hasNewer: false,
}, decodeDetailEvents);
cumulative = installTurnDetailProjectionPage(cumulative.projection, {
  before: "cursor-middle",
  events: [
    processEvent("middle-1", 20),
    processEvent("newer-1", 21),
  ],
  hasMore: true,
  oldestCursor: "cursor-oldest",
  hasNewer: true,
  newerCursor: "cursor-newer",
}, decodeDetailEvents);
cumulative = installTurnDetailProjectionPage(cumulative.projection, {
  before: "cursor-oldest",
  events: [
    processEvent("oldest-1", 10),
    processEvent("middle-1", 11),
  ],
  hasMore: false,
  oldestCursor: null,
  hasNewer: true,
  newerCursor: "cursor-middle",
}, decodeDetailEvents);
assert.deepEqual(
  cumulative.projection.blocks.flatMap((block) =>
    block.kind === "process" ? [block.item_id] : []),
  ["oldest-1", "middle-1", "newer-1", "newer-2"],
  "three cursor-keyed pages accumulate in source order and dedupe overlap",
);
assert.equal(cumulative.projection.hasMore, false);

const page239 = installTurnDetailProjectionPage(undefined, {
  events: Array.from(
    { length: 239 },
    (_, index) => processEvent(`item-${index}`, index),
  ),
  hasMore: false,
}, decodeDetailEvents);
assert.equal(page239.projection.blocks.length, 239,
  "a 239-item authoritative page bypasses the live Turn.blocks=256 window");
const loadedHeavy = installAuthoritativeTurnDetailPage(
  turn("detail-projection"),
  page239.detail!,
  {
    hasMore: false,
    hasNewer: false,
  },
  page239.projection,
);
const summaryAfterDetail = turn("detail-projection", {
  detailEventCount: 239,
  blocks: [],
});
const monotonic = mergeInitialHistory(
  [summaryAfterDetail], [loadedHeavy])[0];
assert.equal(monotonic.detailProjection, page239.projection,
  "a later empty summary cannot erase already-loaded heavyweight detail");
assert.equal(monotonic.detailProjection?.blocks.length, 239);

let capped = installTurnDetailProjectionPage(undefined, {
  events: [
    processEvent("new-1", 30),
    processEvent("new-2", 31),
    processEvent("new-3", 32),
  ],
  hasMore: true,
  oldestCursor: "cap-middle",
}, decodeDetailEvents, { maxItems: 5 });
capped = installTurnDetailProjectionPage(capped.projection, {
  before: "cap-middle",
  events: [
    processEvent("mid-1", 20),
    processEvent("mid-2", 21),
    processEvent("mid-3", 22),
  ],
  hasMore: true,
  oldestCursor: "cap-oldest",
  hasNewer: true,
  newerCursor: "cap-newest",
}, decodeDetailEvents, { maxItems: 5 });
assert.equal(capped.projection.capped, true);
assert.equal(capped.projection.hasMore, true,
  "the memory cap keeps the older source cursor available");
assert.equal(capped.projection.hasNewer, true);
assert.equal(capped.projection.newerCursor, "cap-newest");
assert.deepEqual(capped.projection.blocks.flatMap((block) =>
  block.kind === "process" ? [block.item_id] : []), [
  "mid-1", "mid-2", "mid-3",
], "an older request retains the page the reader explicitly requested");
const returnedToNewer = installTurnDetailProjectionPage(capped.projection, {
  before: "cap-newest",
  events: [
    processEvent("new-1", 30),
    processEvent("new-2", 31),
    processEvent("new-3", 32),
  ],
  hasMore: true,
  oldestCursor: "cap-middle",
  hasNewer: false,
}, decodeDetailEvents, { maxItems: 5 });
assert.equal(returnedToNewer.projection.capped, true);
assert.equal(returnedToNewer.projection.hasMore, true);
assert.equal(returnedToNewer.projection.hasNewer, false);
assert.deepEqual(returnedToNewer.projection.blocks.flatMap((block) =>
  block.kind === "process" ? [block.item_id] : []), [
  "new-1", "new-2", "new-3",
], "the bounded detail window can navigate back to newer process pages");
const cappedDetailTurn = installAuthoritativeTurnDetailPage(
  turn("capped-detail", { detailAutoLoad: true }),
  returnedToNewer.detail!,
  {
    hasMore: returnedToNewer.projection.hasMore,
    oldestCursor: returnedToNewer.projection.oldestCursor,
    hasNewer: returnedToNewer.projection.hasNewer,
    newerCursor: returnedToNewer.projection.newerCursor,
  },
  returnedToNewer.projection,
);
assert.equal(cappedDetailTurn.detailAutoLoad, false,
  "the memory cap stops automatic full-turn pagination");
assert.equal(cappedDetailTurn.detailHasMore, true,
  "stopping automatic pagination must retain the manual older-page cursor");

const loading = markBrowseDetailLoading(
  overlap, "native-2", true, {
    expectedScopeKey: scopeKey,
    expectedViewId: overlap.viewId,
    expectedWindowEpoch: overlap.windowEpoch,
  });
assert.equal(loading.turns[1].detailLoading, true);
assert.equal(loading.windowEpoch, overlap.windowEpoch);
assert.equal(markBrowseDetailLoading(
  loading, "native-2", false, {
    expectedScopeKey: "another-scope",
  }), loading, "a stale detail target cannot mutate the visible browse row");

const cacheMiss = markBrowseNewerUnavailable(tailEviction.projection, {
  expectedScopeKey: scopeKey,
  expectedViewId: tailEviction.projection.viewId,
  expectedWindowEpoch: tailEviction.projection.windowEpoch,
});
assert.equal(cacheMiss.hasNewer, false);
assert.equal(cacheMiss.newerPageKey, null);
assert.deepEqual(cacheMiss.turns, tailEviction.projection.turns,
  "a local newer-page cache miss keeps the readable older window intact");
const failedOlder = settleBrowsePageRequest(initial.projection, {
  expectedScopeKey: scopeKey,
  expectedViewId: initial.projection.viewId,
  expectedWindowEpoch: initial.projection.windowEpoch,
});
assert.equal(failedOlder.windowEpoch, initial.projection.windowEpoch + 1);
assert.deepEqual(failedOlder.turns, initial.projection.turns);
assert.equal(settleBrowsePageRequest(failedOlder, {
  expectedScopeKey: scopeKey,
  expectedViewId: failedOlder.viewId,
  expectedWindowEpoch: initial.projection.windowEpoch,
}), failedOlder, "a second stale failure cannot advance the active window");

const dirty = markBrowseLatestDirty(detailed, {
  expectedScopeKey: scopeKey,
});
assert.equal(dirty.latestDirty, true);
assert.equal(cachedLatestRequiresLiveRuntime(dirty, { isLatest: true }), true);
assert.equal(cachedLatestRequiresLiveRuntime(dirty, { isLatest: false }), false);
assert.equal(cachedLatestRequiresLiveRuntime(detailed, { isLatest: true }), false);
assert.equal(cachedLatestRequiresLiveRuntime(
  { ...dirty, newerPageKey: `latest:${dirty.viewId}` },
  null,
  `latest:${dirty.viewId}`,
), true, "a dirty latest link survives an IndexedDB write/read race");
assert.equal(markBrowseLatestDirty(dirty, {
  expectedScopeKey: scopeKey,
}), dirty);
