import assert from "node:assert/strict";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

import {
  installAuthoritativeTurnDetailPage,
  mergeDetailWithLiveTail,
  mergeAuthoritativeTurnDetail,
  mergeInitialHistory,
  restoreCachedTurnDetails,
} from "../src/history-merge.ts";
import type { ServerEvent } from "../src/protocol.ts";
import type { ProcessBlock, Turn } from "../src/reducer.ts";
import {
  acceptAgentDetail,
  emptyAgentRun,
  projectAgentEvents,
} from "../src/agent-detail.ts";
import type { AgentDetail } from "../src/protocol.ts";
import { installTurnDetailProjectionPage } from "../src/history-detail-projection.ts";

const runningWithCommentary: Turn = {
  id: "running-detail", prompt: "inspect", done: false,
  blocks: [{kind: "text", message_id: "live-progress", channel: "commentary",
    text: "Already visible progress", done: true, liveOrder: 0},
  {kind: "process", item_id: "command-1", processKind: "command",
    title: "Check", phase: "start", status: "running", done: false, liveOrder: 1}],
};
const sourceCommand = {
  ...runningWithCommentary.blocks[1], done: true, status: "succeeded",
} as ProcessBlock;
const installedRunning = installAuthoritativeTurnDetailPage(
  runningWithCommentary, {...runningWithCommentary, blocks: [sourceCommand]},
  {hasMore: true, oldestCursor: "older", hasNewer: false},
  {segments: [], blocks: [sourceCommand], capped: false,
    hasMore: true, oldestCursor: "older", hasNewer: false, newerCursor: null},
);
const runningDisplay = mergeDetailWithLiveTail(
  installedRunning.detailProjection!.blocks, installedRunning.blocks);
assert.equal(runningDisplay.length, 2);
assert.equal(runningDisplay[0].kind, "text",
  "a partial history page must retain live commentary in its original order");
assert.equal(runningDisplay[1].done, true,
  "retaining the live row must not resurrect a completed tool");
assert.equal(installedRunning.done, false);
const settledRunning = installAuthoritativeTurnDetailPage(
  {...installedRunning, done: true}, {...installedRunning, blocks: [sourceCommand]},
  {hasMore: false, hasNewer: false},
  {segments: [], blocks: [sourceCommand], capped: false,
    hasMore: false, oldestCursor: null, hasNewer: false, newerCursor: null},
);
assert.equal(settledRunning.blocks.length, 0,
  "completed source detail retires the provisional live row");

const agentEvents: ServerEvent[] = [{
  v: 37, type: "process", item_id: "nested-agent", kind: "agent",
  phase: "start", status: "running", title: "检查测试", background: true,
  ts: 1,
}];
const agentBlocks = projectAgentEvents(agentEvents);
assert.equal(agentBlocks.length, 1);
assert.equal(agentBlocks[0]?.kind, "process");

const initialAgentDetail: AgentDetail = {
  v: 37, type: "agent_detail", session_id: "session", run_id: "agent-run",
  request_id: "request", revision: "history-revision",
  detail_revision: "agent-revision", authoritative: true,
  title: "审查后端", status: "running", events: agentEvents,
  through_seq: 1, has_more: false, ts: 1,
};
const acceptedAgent = acceptAgentDetail(
  { ...emptyAgentRun("agent-run"), requestId: "request" },
  initialAgentDetail,
);
assert.equal(acceptedAgent.blocks.length, 1);
assert.equal(acceptedAgent.loading, false);
const duplicateLive = acceptAgentDetail(acceptedAgent, {
  ...initialAgentDetail, request_id: null, live: true,
  authoritative: false, events: agentEvents, through_seq: 1,
});
assert.equal(duplicateLive.events.length, 1,
  "a repeated live watermark must not duplicate Agent events");
assert.equal(duplicateLive.detailRevision, "agent-revision",
  "live status-only updates retain the current resident detail revision");
const advancedLive = acceptAgentDetail(duplicateLive, {
  ...initialAgentDetail, request_id: null, live: true,
  authoritative: false, events: [], through_seq: 2,
  detail_revision: "agent-revision-2", status: "succeeded",
});
assert.equal(advancedLive.detailRevision, "agent-revision-2");
assert.equal(advancedLive.status, "succeeded");

const opaqueDirectSummary: Turn = {
  id: "opaque-direct", prompt: "hello", done: true,
  processDetailState: "unknown", detailReasons: [],
  detailEventCount: 0, detailLoaded: false,
  blocks: [{
    kind: "text", message_id: "opaque-direct-final", channel: "final",
    text: "hi", done: true,
  }],
};
const exactDirectDetail: Turn = {
  ...opaqueDirectSummary,
  processDetailState: "none",
  detailLoaded: true,
};
const refinedDirect = mergeAuthoritativeTurnDetail(
  opaqueDirectSummary, exactDirectDetail);
assert.equal(refinedDirect.processDetailState, "none",
  "authoritative final-only detail refines an opaque summary to direct reply");
const directAfterOpaqueRefresh = mergeInitialHistory(
  [opaqueDirectSummary], [refinedDirect])[0];
assert.equal(directAfterOpaqueRefresh.processDetailState, "none",
  "same-revision opaque refresh cannot resurrect a dismissed process shell");

const stableKnownProcess = mergeAuthoritativeTurnDetail({
  ...opaqueDirectSummary,
  id: "known-process",
  processDetailState: "present",
  detailReasons: ["process"],
  detailEventCount: 4,
}, {
  ...exactDirectDetail,
  id: "known-process",
});
assert.equal(stableKnownProcess.processDetailState, "present",
  "a final-only replacement page cannot erase known process presence");

const stableLiveKnownProcess = mergeInitialHistory([{
  ...exactDirectDetail,
  id: "live-known-process",
}], [{
  ...exactDirectDetail,
  id: "live-known-process",
  processDetailState: "present",
  detailReasons: ["process"],
  detailLoaded: false,
}])[0];
assert.equal(stableLiveKnownProcess.processDetailState, "present",
  "an exact earlier snapshot cannot erase later same-revision process evidence");

const sourceTimedProcess: Turn = {
  ...exactDirectDetail,
  id: "source-timed-process",
  processDetailState: "present",
  detailReasons: ["process"],
  processStartedTs: 10_000,
  processDoneTs: 14_000,
};
const processAfterStaleZeroCache = mergeInitialHistory(
  [sourceTimedProcess],
  [{
    ...sourceTimedProcess,
    processStartedTs: 90_000,
    processDoneTs: 90_000,
  }],
)[0];
assert.equal(processAfterStaleZeroCache.processStartedTs, 10_000);
assert.equal(processAfterStaleZeroCache.processDoneTs, 14_000,
  "a stale parser-time zero interval cannot stretch source-backed timing");

const sourceBoundedProcess = { ...sourceTimedProcess, ts: 5_000, doneTs: 15_000 };
const correctedClock = mergeInitialHistory([sourceBoundedProcess], [{
  ...sourceBoundedProcess, processStartedTs: 10_000, processDoneTs: 99_000,
}])[0];
assert.equal(correctedClock.processDoneTs, 14_000,
  "cached reconstruction time beyond the source terminal must not win a max merge");

const finalOnlySegment: Turn = {
  ...exactDirectDetail, id: "final-only-steer", clientMsgId: "client-final",
  forkPointId: "native-task", done: true, processDetailState: "none", detailEventCount: 0,
};
const falseProcessClaim: Turn = {
  ...finalOnlySegment, processDetailState: "present", detailReasons: ["process"],
  detailEventCount: 1, processStartedTs: 99_000,
};
const correctedFinalSummary = mergeInitialHistory([finalOnlySegment], [falseProcessClaim], {}, true)[0];
assert.equal(correctedFinalSummary.processDetailState, "none",
  "a native fork point does not protect a false metadata-only process claim");
const correctedFinalDetail = installAuthoritativeTurnDetailPage(
  falseProcessClaim, finalOnlySegment, {hasMore: false, hasNewer: false},
);
assert.equal(correctedFinalDetail.processDetailState, "none");
assert.equal(correctedFinalDetail.detailEventCount, 0);
assert.equal(correctedFinalDetail.processStartedTs, undefined);
assert.equal(correctedFinalDetail.detailError, undefined);
for (const page of [{hasMore: true, hasNewer: false}, {hasMore: false, hasNewer: true}]) {
  assert.equal(installAuthoritativeTurnDetailPage(falseProcessClaim, finalOnlySegment, page)
    .processDetailState, "present", "an unread adjacent page is not evidence of absence");
}
assert.equal(installAuthoritativeTurnDetailPage(
  {...falseProcessClaim, done: false}, finalOnlySegment, {hasMore: false, hasNewer: false},
).processDetailState, "present", "an active response can still produce process content");

const partialDirectDetail = installAuthoritativeTurnDetailPage(
  { ...opaqueDirectSummary, id: "partial-direct" },
  { ...exactDirectDetail, id: "partial-direct" },
  {
    hasMore: true,
    oldestCursor: "older-process-page",
    hasNewer: false,
  },
);
assert.equal(partialDirectDetail.processDetailState, "unknown",
  "a final-only bounded page cannot prove unread older pages have no process");
assert.equal(partialDirectDetail.detailLoaded, false);
assert.equal(partialDirectDetail.detailRetryDirection, "older");
assert.equal(partialDirectDetail.detailRetryBefore, "older-process-page");

const legacyUnknownEnvelope = mergeInitialHistory([{
  ...exactDirectDetail,
  id: "legacy-unknown-envelope",
  blocks: [{
    kind: "text", message_id: "legacy-unknown-text", channel: "unknown",
    text: "compatibility answer", done: true,
  }],
}], [{
  ...exactDirectDetail,
  id: "legacy-unknown-envelope",
  blocks: [],
}])[0];
assert.equal(legacyUnknownEnvelope.processDetailState, "none",
  "an unclassified compatibility answer cannot manufacture a process shell");

const restoredUnknownProcess = restoreCachedTurnDetails([{
  ...opaqueDirectSummary,
  id: "restored-unknown-process",
}], [{
  ...opaqueDirectSummary,
  id: "restored-unknown-process",
  processDetailState: "present",
  detailReasons: ["process"],
  blocks: [{
    kind: "tool", message_id: "restored-tool-message",
    tool_use_id: "restored-tool", tool: "Read", input: {}, done: true,
  }],
}], "provisional")[0];
assert.equal(restoredUnknownProcess.processDetailState, "present");
assert.equal(restoredUnknownProcess.detailProjection?.blocks.length, 1,
  "an opaque zero-count summary restores same-revision cached process");

const staleLoadedUnknown = mergeInitialHistory([{
  ...opaqueDirectSummary,
  id: "stale-loaded-unknown",
}], [{
  ...opaqueDirectSummary,
  id: "stale-loaded-unknown",
  detailLoaded: true,
}])[0];
assert.equal(staleLoadedUnknown.detailLoaded, false,
  "a stale loaded bit cannot suppress opaque zero-count detail");

const harness = await createServer({
  root: process.cwd(),
  appType: "custom",
  logLevel: "silent",
  server: { middlewareMode: true, watch: null },
});

try {
  const { createRuntime, initialState, reduce } =
    await harness.ssrLoadModule("/src/reducer.ts");
  const { ChatView } = await harness.ssrLoadModule(
    "/src/components/ChatView.tsx");
  const { ProcessTimeline } = await harness.ssrLoadModule(
    "/src/components/ProcessTimeline.tsx");
  const { BtwPanel } = await harness.ssrLoadModule(
    "/src/components/BtwPanel.tsx");
  const { AgentDetailPanel } = await harness.ssrLoadModule(
    "/src/components/AgentDetailPanel.tsx");
  const { activeTurnCandidateIds, displayActiveTurnOwnerId, generatedOutputImages } =
    await harness.ssrLoadModule("/src/process-blocks.ts");
  const generated: ProcessBlock = {
    kind: "process", processKind: "server_tool", tool: "image_generation",
    item_id: "canonical", phase: "end", status: "succeeded", done: true,
    title: "生成图片",
    input: { history_image: { image_id: "img-content-native-1" } },
  };
  const liveGenerated = { ...generated, item_id: "live", input: {
    ...generated.input, preview_id: "snapshot-id",
  } };
  assert.deepEqual(generatedOutputImages([generated, liveGenerated]), [liveGenerated]);
  assert.deepEqual(generatedOutputImages([liveGenerated, generated]), [liveGenerated]);
  assert.equal(generatedOutputImages([generated, { ...generated,
    input: { history_image: { image_id: "img-different-native-or-content" } },
  }]).length, 2, "distinct native/content image references remain distinct");
  assert.deepEqual(generatedOutputImages([{ ...generated, done: false },
    { ...generated, status: "failed" }]), []);
  assert.equal(generatedOutputImages(Array.from({ length: 12 }, (_, index) => ({
    ...generated, input: { history_image: { image_id: `img-${index}` } },
  }))).length, 8, "the generated gallery stays bounded");
  const imageSummary: Turn = {
    ...sourceTimedProcess, blocks: [generated],
  };
  const imageFreeDetail: Turn = {
    ...imageSummary, blocks: exactDirectDetail.blocks,
  };
  for (const projection of [undefined, {
    segments: [], blocks: imageFreeDetail.blocks, capped: false,
    hasMore: true, oldestCursor: "older", hasNewer: false, newerCursor: null,
  }]) {
    const paged = installAuthoritativeTurnDetailPage(
      imageSummary, imageFreeDetail,
      { hasMore: true, oldestCursor: "older", hasNewer: false }, projection,
    );
    assert.equal(generatedOutputImages(paged.blocks).length, 1,
      "an image-free process detail page must not erase a known output image");
    assert.equal(paged.processStartedTs, imageSummary.processStartedTs);
    assert.equal(paged.processDoneTs, imageSummary.processDoneTs);
  }
  const event = (body: Record<string, unknown>): ServerEvent => ({
    v: 37, ts: 10, ...body,
  } as ServerEvent);

  const decodeSeam = (events: ServerEvent[]): Turn | undefined => {
    let state = { ...initialState, focusedSid: "seam", runtimes: { seam: createRuntime() } };
    for (const item of events) state = reduce(state, {
      type: "event", event: { ...item, sid: "seam" },
    });
    return state.runtimes.seam.turns.find((turn: Turn) => turn.id === "human");
  };
  const seamUse = event({ type: "tool_use", message_id: "message", tool_use_id: "call",
    tool: "shell", category: "command", input: { command: "ls" }, title: "List files" });
  const seamUser = event({ type: "user_msg", msg_id: "human", prompt: "inspect" });
  const newestSeam = installTurnDetailProjectionPage(undefined, {
    events: [seamUser, event({ ...seamUse, tool: "tool", category: "tool", input: {}, title: null }),
      event({ type: "tool_result", tool_use_id: "call", content: "files", is_error: false })],
    hasMore: true, oldestCursor: "older",
  }, decodeSeam);
  const joinedSeam = installTurnDetailProjectionPage(newestSeam.projection, {
    before: "older", events: [seamUser, seamUse], hasMore: false,
    hasNewer: true, newerCursor: "newer",
  }, decodeSeam).projection.blocks;
  assert.equal(joinedSeam.length, 1, "one call spanning byte windows stays one tool");
  assert.equal(joinedSeam[0].kind === "tool" && joinedSeam[0].tool, "shell");
  assert.deepEqual(joinedSeam[0].kind === "tool" && joinedSeam[0].input, { command: "ls" });
  assert.equal(joinedSeam[0].kind === "tool" && joinedSeam[0].result?.content, "files");
  assert.equal(joinedSeam[0].done, true);

  let multiBtwState = reduce(initialState, {
    type: "event", event: event({
      type: "btw_opened", sid: "btw-one", request_id: "open-one",
      btw_sid: "btw-one", parent_sid: "parent", engine: "codex",
      created_at: 1, revision: 1,
    }),
  });
  multiBtwState = reduce(multiBtwState, {
    type: "event", event: event({
      type: "btw_opened", sid: "btw-two", request_id: "open-two",
      btw_sid: "btw-two", parent_sid: "parent", engine: "codex",
      created_at: 2, revision: 2,
    }),
  });
  assert.deepEqual(
    multiBtwState.btwByParentSid.parent.chats.map(
      (chat: { sid: string }) => chat.sid),
    ["btw-one", "btw-two"],
    "opening another BTW appends a side chat instead of replacing the first",
  );
  assert.equal(multiBtwState.btwByParentSid.parent.activeSid, "btw-two");
  multiBtwState = reduce(multiBtwState, {
    type: "select_btw", parentSid: "parent", btwSid: "btw-one",
  });
  multiBtwState = reduce(multiBtwState, {
    type: "event", event: event({
      type: "btw_opened", sid: "btw-two", request_id: "open-two",
      btw_sid: "btw-two", parent_sid: "parent", engine: "codex",
      created_at: 2, revision: 2,
    }),
  });
  assert.equal(multiBtwState.btwByParentSid.parent.activeSid, "btw-one",
    "a duplicate open response cannot steal the user's selected side chat");
  multiBtwState = reduce(multiBtwState, {
    type: "event", event: event({
      type: "btw_sync", generation: "wrapper-one", revision: 2,
      sessions: [
        { btw_sid: "btw-two", parent_sid: "parent", engine: "codex", created_at: 2 },
        { btw_sid: "btw-one", parent_sid: "parent", engine: "codex", created_at: 1 },
      ],
    }),
  });
  assert.equal(multiBtwState.btwByParentSid.parent.activeSid, "btw-one",
    "an equal/new authoritative refresh catalog preserves the selected tab");
  multiBtwState = reduce(multiBtwState, {
    type: "event", event: event({
      type: "snapshot", sid: "btw-one", cc_session_id: null,
      state: "idle", tail_text: "", generation: "wrapper-one",
    }),
  });
  multiBtwState = reduce(multiBtwState, {
    type: "event", event: event({
      type: "state", sid: "btw-one", state: "running", seq: 1,
    }),
  });
  multiBtwState = reduce(multiBtwState, {
    type: "event", event: event({
      type: "btw_sync", generation: "wrapper-one", revision: 3,
      sessions: [
        { btw_sid: "btw-two", parent_sid: "parent", engine: "codex",
          created_at: 2, state: "idle" },
        { btw_sid: "btw-one", parent_sid: "parent", engine: "codex",
          created_at: 1, state: "idle" },
      ],
    }),
  });
  assert.equal(multiBtwState.runtimes["btw-one"].state, "running",
    "a catalog snapshot cannot overwrite newer live state on a synced socket");
  multiBtwState = reduce(multiBtwState, {
    type: "conn", connState: "reconnecting",
  });
  multiBtwState = reduce(multiBtwState, {
    type: "event", event: event({
      type: "btw_sync", generation: "wrapper-one", revision: 4,
      sessions: [
        { btw_sid: "btw-two", parent_sid: "parent", engine: "codex",
          created_at: 2, state: "idle" },
        { btw_sid: "btw-one", parent_sid: "parent", engine: "codex",
          created_at: 1, state: "idle" },
      ],
    }),
  });
  assert.equal(multiBtwState.runtimes["btw-one"].state, "idle",
    "authoritative BTW sync repairs state missed while the tab was offline");
  const retainedSideTurn = {
    id: "retained-side-turn", prompt: "older side question",
    blocks: [], done: true, ts: 1,
  };
  multiBtwState = reduce(multiBtwState, {
    type: "set_turns", sid: "btw-one", turns: [retainedSideTurn],
  });
  multiBtwState = reduce(multiBtwState, {
    type: "event", event: event({
      type: "replay_start", sid: "btw-one", from_seq: 10, to_seq: 20,
      truncated: true, generation: "wrapper-one",
    }),
  });
  assert.deepEqual(multiBtwState.runtimes["btw-one"].turns,
    [retainedSideTurn],
    "a truncated ephemeral replay keeps its bounded projection instead of "
      + "waiting forever for an unavailable History endpoint");
  assert.equal(multiBtwState.runtimes["btw-one"].historyInvalidated, false);
  assert.equal(multiBtwState.historyRecovery, null);
  multiBtwState = reduce(multiBtwState, {
    type: "event", event: event({
      type: "replay_end", sid: "btw-one", to_seq: 20, truncated: true,
    }),
  });
  multiBtwState = reduce(multiBtwState, {
    type: "event", event: event({
      type: "btw_closed", sid: "btw-one", btw_sid: "btw-one",
      parent_sid: "parent", revision: 5,
    }),
  });
  assert.deepEqual(multiBtwState.btwByParentSid.parent, {
    chats: [{
      sid: "btw-two", engine: "codex", createdAt: 2, state: "idle",
    }],
    activeSid: "btw-two",
  }, "closing the selected tab chooses the remaining neighbor");
  assert.equal("btw-one" in multiBtwState.runtimes, false);
  multiBtwState = reduce(multiBtwState, {
    type: "event", event: event({
      type: "snapshot", sid: "btw-one", cc_session_id: null,
      state: "idle", tail_text: "", generation: "old",
    }),
  });
  multiBtwState = reduce(multiBtwState, {
    type: "event", event: event({
      type: "btw_opened", sid: "btw-one", request_id: "stale-open",
      btw_sid: "btw-one", parent_sid: "parent", engine: "codex",
      created_at: 1, revision: 2,
    }),
  });
  assert.equal("btw-one" in multiBtwState.runtimes, false,
    "late replay and an older open revision cannot resurrect a closed chat");
  const closedCatalogState = reduce(multiBtwState, {
    type: "event", event: event({
      type: "btw_closed", sid: "btw-two", btw_sid: "btw-two",
      parent_sid: "parent", revision: 6,
    }),
  });
  assert.equal(closedCatalogState.btwRevision, 6);
  assert.equal(reduce(closedCatalogState, {
    type: "clear_all_btw",
  }).btwRevision, 0,
  "a wrapper generation change resets revision even after every tab closed");

  const agentPanelMarkup = renderToStaticMarkup(createElement(
    AgentDetailPanel,
    {
      run: acceptedAgent,
      canGoBack: false,
      onBack: () => {},
      onClose: () => {},
      onRetry: () => {},
      onLoadEarlier: () => {},
      onOpenAgent: () => {},
    },
  ));
  assert.match(agentPanelMarkup,
    /aria-label="调整协作代理面板宽度"/);
  assert.match(agentPanelMarkup, /data-lock-horizontal-swipe="true"/);

  const btwRuntime = createRuntime();
  btwRuntime.state = "idle";
  btwRuntime.syncReady = true;
  btwRuntime.liveOwner = { turnId: "btw-previous-turn", seq: 8 };
  btwRuntime.acceptancePending = "btw-new-turn";
  btwRuntime.pendingQuestion = {
    ask_id: "btw-question", question: "继续侧边任务吗？",
    options: [{ label: "继续" }, { label: "停止" }],
  };
  btwRuntime.turns = [{
    id: "btw-previous-turn", prompt: "previous prompt", done: true,
    blocks: [{
      kind: "process", item_id: "btw-successful-hook", processKind: "hook",
      phase: "end", status: "succeeded", title: "BTW hook plumbing",
      done: true,
    }, {
      kind: "process", item_id: "btw-failed-hook", processKind: "hook",
      phase: "end", status: "failed", title: "BTW hook failure",
      done: true,
    }],
  }, {
    id: "btw-new-turn", prompt: "new prompt", done: false, blocks: [],
  }];
  const btwMarkup = renderToStaticMarkup(createElement(BtwPanel, {
    sid: "btw-session", rt: btwRuntime, engine: "codex", opening: false,
    chats: [{
      sid: "btw-session", engine: "codex", title: "侧聊 1", state: "idle",
      needsAnswer: true,
    }],
    active: "btw", hasArtifact: false, catalog: {},
    draftKey: "btw-session-draft",
    draftStore: {
      get: () => ({ input: "", images: [], files: [], pastes: [] }),
      set: () => {},
    },
    sendMode: "steer", unconfirmedQueued: [], unconfirmedReplaceable: [],
    queueCapacity: {}, replaceQueueCapacity: {}, onTab: () => {},
    onNew: () => {}, onSelect: () => {}, onCloseChat: () => {},
    onSend: () => true, onSteer: () => true, onInterrupt: () => {},
    onSetSendMode: () => {}, onEnqueue: () => true,
    onSetPending: () => true, onRemoveQueued: () => {},
    onInspectQueued: () => {}, onSetModel: () => {}, onSetEffort: () => {},
    onSetAutoCompact: () => true, onAnswerQuestion: () => {},
    onCollapse: () => {}, onDismissNotice: () => {},
  }));
  assert.equal((btwMarkup.match(/思考中/g) ?? []).length, 1);
  assert.ok(btwMarkup.indexOf("思考中") > btwMarkup.indexOf("new prompt"),
    "BTW paints the pending submit spark on the new row, not its stale owner");
  assert.doesNotMatch(btwMarkup, /BTW hook plumbing/,
    "successful Codex hook plumbing stays hidden in the side chat");
  assert.match(btwMarkup, /1 项/,
    "the actionable Codex hook failure remains in the collapsed side-chat count");
  assert.match(btwMarkup, /待回答/);
  assert.match(btwMarkup, /继续侧边任务吗？/,
    "a pending BTW question is actionable inside the side panel");

  const claudeEchoGapSid = "claude-user-echo-keeps-working-owner";
  const claudeEchoGapMessage = "claude-browser-message";
  let claudeEchoGapState = reduce({
    ...initialState,
    focusedSid: claudeEchoGapSid,
    runtimes: {
      [claudeEchoGapSid]: {
        ...createRuntime(), state: "idle" as const, syncReady: true,
        turns: [{
          id: "claude-previous-turn", prompt: "previous", blocks: [],
          done: true,
        }],
        liveOwner: { turnId: "claude-previous-turn", seq: 10 },
      },
    },
  }, {
    type: "query_sent", sid: claudeEchoGapSid,
    prompt: "keep the spark visible", msg_id: claudeEchoGapMessage,
    ts: 10_000,
  });
  claudeEchoGapState = reduce(claudeEchoGapState, {
    type: "event", event: event({
      type: "state", sid: claudeEchoGapSid, seq: 11, state: "running",
    }),
  });
  const activeOwner = () => {
    const runtime = claudeEchoGapState.runtimes[claudeEchoGapSid];
    return activeTurnCandidateIds(
      runtime.turns,
      displayActiveTurnOwnerId(
        runtime.liveOwner?.turnId, runtime.acceptancePending),
      runtime.state !== "idle" || !!runtime.acceptancePending,
    );
  };
  assert.deepEqual(activeOwner(), [claudeEchoGapMessage],
    "the optimistic Claude submit owns the working spark before its echo");
  claudeEchoGapState = reduce(claudeEchoGapState, {
    type: "event", event: event({
      type: "user_msg", sid: claudeEchoGapSid, seq: 12,
      msg_id: claudeEchoGapMessage, prompt: "keep the spark visible",
    }),
  });
  assert.equal(
    claudeEchoGapState.runtimes[claudeEchoGapSid].acceptancePending,
    null,
    "the authoritative Claude user echo still releases the submit latch",
  );
  assert.equal(
    claudeEchoGapState.runtimes[claudeEchoGapSid].liveOwner?.turnId,
    claudeEchoGapMessage,
    "the same echo atomically transfers display ownership to the accepted row",
  );
  assert.deepEqual(activeOwner(), [claudeEchoGapMessage],
    "the spark stays visible while Claude's TurnBinding is pending");
  claudeEchoGapState = reduce(claudeEchoGapState, {
    type: "event", event: event({
      type: "turn_binding", sid: claudeEchoGapSid, seq: 13,
      msg_id: claudeEchoGapMessage, turn_id: "claude-native-user",
    }),
  });
  assert.deepEqual(activeOwner(), [claudeEchoGapMessage],
    "the later native binding keeps the visible Claude owner stable");

  const claudeHistoryGapSid = "claude-history-acceptance-keeps-owner";
  const claudeHistoryGapMessage = "claude-history-browser-message";
  const claudeHistoryGapNative = "claude-history-native-message";
  const claudeHistoryGapGeneration = "claude-history-generation";
  let claudeHistoryGapBase = reduce({
    ...initialState,
    focusedSid: claudeHistoryGapSid,
    runtimes: {
      [claudeHistoryGapSid]: {
        ...createRuntime(), state: "idle" as const, syncReady: true,
        controlGeneration: claudeHistoryGapGeneration,
        historyGeneration: claudeHistoryGapGeneration,
        historyRevision: "claude-history-revision",
        historyBuildSeq: 1,
        historyLiveSeq: 10,
        historyNewestId: "claude-history-previous",
        liveOwner: { turnId: "claude-history-previous", seq: 10 },
        turns: [{
          id: "claude-history-previous", prompt: "previous", blocks: [],
          done: true,
        }],
      },
    },
  }, {
    type: "query_sent", sid: claudeHistoryGapSid,
    prompt: "survive a lost user echo", msg_id: claudeHistoryGapMessage,
    ts: 20_000,
  });
  claudeHistoryGapBase = reduce(claudeHistoryGapBase, {
    type: "event", event: event({
      type: "state", sid: claudeHistoryGapSid, seq: 11, state: "running",
    }),
  });
  const acceptedHistoryTurn = {
    id: claudeHistoryGapNative,
    clientMsgId: claudeHistoryGapMessage,
    prompt: "survive a lost user echo",
    done: false,
    blocks: [{
      kind: "text" as const,
      message_id: "claude-history-final",
      text: "finished answer",
      done: false,
      channel: "final" as const,
    }],
    detailEventCount: 0,
    detailLoaded: false,
  };
  const historyOnlyAcceptance = (
    overrides: Record<string, unknown> = {},
  ): ServerEvent => event({
    type: "history", session_id: claudeHistoryGapSid,
    revision: "claude-history-revision",
    generation: claudeHistoryGapGeneration,
    build_seq: 2, live_seq: 12,
    detail: "summary", authoritative: true,
    in_progress: true, external: false, has_more: false,
    newest_id: claudeHistoryGapNative,
    events: [], turns: [acceptedHistoryTurn],
    ...overrides,
  });
  const activeHistoryOwner = (runtime: ReturnType<typeof createRuntime>) =>
    activeTurnCandidateIds(
      runtime.turns,
      displayActiveTurnOwnerId(
        runtime.liveOwner?.turnId, runtime.acceptancePending),
      runtime.state !== "idle" || runtime.mirroredRunning
        || !!runtime.acceptancePending,
    );
  let claudeHistoryGapState = reduce(claudeHistoryGapBase, {
    type: "event", event: historyOnlyAcceptance(),
  });
  let claudeHistoryGapRuntime =
    claudeHistoryGapState.runtimes[claudeHistoryGapSid];
  assert.equal(claudeHistoryGapRuntime.acceptancePending, null,
    "authoritative History confirms the optimistic submit without its live echo");
  assert.deepEqual(claudeHistoryGapRuntime.liveOwner, {
    turnId: claudeHistoryGapMessage, seq: 12,
  }, "current running History transfers ownership to its exact accepted head");
  assert.deepEqual(activeHistoryOwner(claudeHistoryGapRuntime),
    [claudeHistoryGapMessage],
    "History-only acceptance keeps the working spark on the submitted row");
  claudeHistoryGapState = reduce(claudeHistoryGapState, {
    type: "event", event: event({
      type: "turn_end", sid: claudeHistoryGapSid, seq: 13,
      turn_id: claudeHistoryGapNative,
      result: { subtype: "success", duration_ms: 20, is_error: false },
    }),
  });
  claudeHistoryGapRuntime =
    claudeHistoryGapState.runtimes[claudeHistoryGapSid];
  assert.equal(claudeHistoryGapRuntime.turns[0].done, true,
    "a live terminal closes the current turn recovered only from History");
  const recoveredCompletionMarkup = renderToStaticMarkup(createElement(
    ChatView,
    {
      sid: claudeHistoryGapSid,
      turns: claudeHistoryGapRuntime.turns,
      engine: "claude",
      onEdit: () => {},
      onGetDiff: () => {},
    },
  ));
  assert.match(recoveredCompletionMarkup, /class="turn-done-mark"/,
    "the recovered Claude terminal paints the final completion spark");
  claudeHistoryGapState = reduce(claudeHistoryGapState, {
    type: "event", event: event({
      type: "state", sid: claudeHistoryGapSid, seq: 14, state: "idle",
    }),
  });
  assert.deepEqual(activeHistoryOwner(
    claudeHistoryGapState.runtimes[claudeHistoryGapSid]), [],
  "a later idle lifecycle still stops the History-recovered spark");

  const staleHistoryOwner = reduce(claudeHistoryGapBase, {
    type: "event", event: historyOnlyAcceptance({ live_seq: 10 }),
  }).runtimes[claudeHistoryGapSid];
  assert.notEqual(staleHistoryOwner.liveOwner?.turnId, claudeHistoryGapMessage,
    "a History watermark behind live lifecycle cannot steal ownership");

  const oldGenerationOwner = reduce(claudeHistoryGapBase, {
    type: "event", event: historyOnlyAcceptance({
      generation: "claude-history-old-generation",
    }),
  }).runtimes[claudeHistoryGapSid];
  assert.notEqual(oldGenerationOwner.liveOwner?.turnId, claudeHistoryGapMessage,
    "a History response cannot self-install an old generation as owner proof");

  const externalHistoryOwner = reduce(claudeHistoryGapBase, {
    type: "event", event: historyOnlyAcceptance({ external: true }),
  }).runtimes[claudeHistoryGapSid];
  assert.notEqual(externalHistoryOwner.liveOwner?.turnId,
    claudeHistoryGapMessage,
    "external session activity cannot claim a browser-managed optimistic row");

  const otherHistoryHead = "claude-history-other-head";
  const nonHeadHistoryOwner = reduce(claudeHistoryGapBase, {
    type: "event", event: historyOnlyAcceptance({
      newest_id: otherHistoryHead,
      turns: [acceptedHistoryTurn, {
        id: otherHistoryHead, prompt: "newer external prompt", done: false,
        blocks: [], detailEventCount: 0, detailLoaded: false,
      }],
    }),
  }).runtimes[claudeHistoryGapSid];
  assert.notEqual(nonHeadHistoryOwner.liveOwner?.turnId,
    claudeHistoryGapMessage,
    "session-wide running state cannot reactivate an accepted non-head row");

  const slowProcessSid = "slow-process-clock";
  let slowProcessState = reduce({
    ...initialState,
    focusedSid: slowProcessSid,
    runtimes: {
      [slowProcessSid]: {
        ...createRuntime(), state: "running" as const, syncReady: true,
      },
    },
  }, {
    type: "query_sent", sid: slowProcessSid, prompt: "wait first",
    msg_id: "slow-process-turn", ts: 1_000,
  });
  for (const slowEvent of [
    event({
      type: "tool_use", sid: slowProcessSid, ts: 41,
      message_id: "slow-process-message", tool_use_id: "slow-process-tool",
      tool: "shell", input: { command: "true" },
    }),
    event({
      type: "tool_result", sid: slowProcessSid, ts: 42,
      tool_use_id: "slow-process-tool", content: "", is_error: false,
      status: "succeeded",
    }),
    event({
      type: "turn_end", sid: slowProcessSid, ts: 43,
      turn_id: "slow-process-turn",
      result: { subtype: "success", duration_ms: 42_000, is_error: false },
    }),
  ]) {
    slowProcessState = reduce(slowProcessState, {
      type: "event", event: slowEvent,
    });
  }
  const slowProcessTurn = slowProcessState.runtimes[slowProcessSid].turns[0];
  assert.equal(slowProcessTurn.processStartedTs, 41_000);
  assert.equal(slowProcessTurn.processDoneTs, 42_000,
    "the process clock freezes when the last visible tool settles");
  const slowProcessMarkup = renderToStaticMarkup(createElement(ChatView, {
    sid: slowProcessSid, turns: [slowProcessTurn], engine: "codex",
    onEdit: () => {}, onGetDiff: () => {},
  }));
  assert.match(slowProcessMarkup, /已处理 1s/);
  assert.doesNotMatch(slowProcessMarkup, /已处理 42s/,
    "a late first process event never inherits the user-message wait");

  const diffSid = "diff-snapshot-clock";
  let diffState = reduce({
    ...initialState,
    focusedSid: diffSid,
    runtimes: {
      [diffSid]: {
        ...createRuntime(), state: "running" as const, syncReady: true,
      },
    },
  }, {
    type: "query_sent", sid: diffSid, prompt: "edit",
    msg_id: "diff-turn", ts: 1_000,
  });
  diffState = reduce(diffState, { type: "event", event: event({
    type: "turn_diff", sid: diffSid, ts: 41,
    item_id: "diff-snapshot", diff: "+done",
  }) });
  const diffTurn = diffState.runtimes[diffSid].turns[0];
  const diffBlock = diffTurn.blocks.find((block: { kind: string }) =>
    block.kind === "process");
  assert.equal(diffBlock?.done, true);
  assert.equal(diffTurn.processStartedTs, 41_000);
  assert.equal(diffTurn.processDoneTs, 41_000,
    "a complete diff snapshot does not run the process clock until TurnEnd");

  const stableSid = "stable-known-process";
  let stableState = {
    ...initialState,
    focusedSid: stableSid,
    runtimes: {
      [stableSid]: {
        ...createRuntime(),
        historyRevision: "stable-r1",
        turns: [{
          id: "stable-turn", prompt: "inspect", done: true,
          processDetailState: "present" as const,
          detailReasons: ["process" as const],
          detailEventCount: 9, detailLoaded: false, blocks: [],
        }],
      },
    },
  };
  stableState = reduce(stableState, { type: "event", event: event({
    type: "turn_detail", session_id: stableSid, turn_id: "stable-turn",
    revision: "stable-r1", events: [], before: null,
    has_more: false, has_newer: false, authoritative: true,
  }) });
  const stableAfterEmpty = stableState.runtimes[stableSid].turns[0];
  assert.equal(stableAfterEmpty.processDetailState, "present",
    "an empty raced detail response cannot erase known process evidence");
  assert.equal(stableAfterEmpty.detailEventCount, 9);
  assert.equal(stableAfterEmpty.detailLoaded, false);

  const unknownSid = "unknown-empty-detail";
  let unknownState = {
    ...initialState,
    focusedSid: unknownSid,
    runtimes: {
      [unknownSid]: {
        ...createRuntime(),
        historyRevision: "unknown-r1",
        turns: [{
          ...opaqueDirectSummary,
          id: "unknown-turn",
        }],
      },
    },
  };
  unknownState = reduce(unknownState, { type: "event", event: event({
    type: "turn_detail", session_id: unknownSid, turn_id: "unknown-turn",
    revision: "unknown-r1", events: [], before: null,
    has_more: false, has_newer: false, authoritative: true,
  }) });
  const unknownAfterEmpty = unknownState.runtimes[unknownSid].turns[0];
  assert.equal(unknownAfterEmpty.processDetailState, "none",
    "an exact empty detail response still refines an opaque direct answer");
  assert.equal(unknownAfterEmpty.detailLoaded, true);

  const unknownDetailMarkup = renderToStaticMarkup(createElement(ChatView, {
    sid: "unknown-detail-session", turns: [opaqueDirectSummary],
    engine: "codex", onEdit: () => {}, onGetDiff: () => {},
    onLoadDetail: () => {},
  }));
  assert.doesNotMatch(unknownDetailMarkup, /已处理/,
    "an opaque native summary must not claim that a process exists");
  assert.doesNotMatch(unknownDetailMarkup,
    /查看本轮详情|查看完整内容|turn-detail-entry/,
    "uncertainty alone must not create an empty disclosure for a direct reply");

  const pagedUnknownMarkup = renderToStaticMarkup(createElement(ChatView, {
    sid: "paged-unknown-session", turns: [partialDirectDetail],
    engine: "codex", onEdit: () => {}, onGetDiff: () => {},
    onLoadDetail: () => {},
  }));
  assert.match(pagedUnknownMarkup, /查看更多内容/,
    "an actual unread detail page remains reachable without an empty placeholder");

  const cursorlessMarkup = renderToStaticMarkup(createElement(ChatView, {
    sid: "cursorless-detail-session", turns: [{
      ...opaqueDirectSummary, detailHasMore: true, detailHasNewer: true,
    }], engine: "codex", onEdit: () => {}, onGetDiff: () => {},
    onLoadDetail: () => {},
  }));
  assert.doesNotMatch(cursorlessMarkup, /查看更多内容|turn-detail-entry/,
    "stale pagination flags without a cursor cannot offer a repeating initial read");

  const partiallyRestoredProcess: Turn = {
    ...sourceTimedProcess,
    blocks: [generated, ...exactDirectDetail.blocks],
    detailLoaded: false, detailRestoreIncomplete: true,
    detailHasMore: true, detailOldestCursor: "older-process-page",
    detailEventCount: 433,
    processDoneTs: sourceTimedProcess.processStartedTs! + 129 * 60_000,
  };
  const partiallyRestoredMarkup = renderToStaticMarkup(createElement(ChatView, {
    sid: "partially-restored-session", turns: [partiallyRestoredProcess],
    engine: "codex", onEdit: () => {}, onGetDiff: () => {},
    onLoadDetail: () => {},
  }));
  assert.match(partiallyRestoredMarkup, /已处理 2h 9m/);
  assert.match(partiallyRestoredMarkup, /generated-image-gallery/,
    "output images stay outside the collapsed process disclosure");
  assert.doesNotMatch(partiallyRestoredMarkup, /查看更多内容|turn-detail-entry/,
    "a known process owns its pagination without a second inert entry below the answer");

  const processWithTruncatedAnswerMarkup = renderToStaticMarkup(createElement(ChatView, {
    sid: "process-with-truncated-answer", turns: [{
      ...partiallyRestoredProcess, detailReasons: ["process", "answer_truncated"],
    }], engine: "codex", onEdit: () => {}, onGetDiff: () => {},
    onLoadDetail: () => {},
  }));
  assert.match(processWithTruncatedAnswerMarkup, /查看完整内容/,
    "real deferred answer content retains its own entry even beside a process disclosure");

  const restoredProcessMarkup = renderToStaticMarkup(createElement(ChatView, {
    sid: "restored-process-session", turns: [restoredUnknownProcess],
    engine: "codex", onEdit: () => {}, onGetDiff: () => {},
    onLoadDetail: () => {},
  }));
  assert.match(restoredProcessMarkup, /已处理/,
    "cached concrete process evidence still refines and renders an opaque summary");

  const failedUnknownMarkup = renderToStaticMarkup(createElement(ChatView, {
    sid: "failed-unknown-session", turns: [{
      ...opaqueDirectSummary, detailError: "详细过程暂时不可用",
    }], engine: "codex", onEdit: () => {}, onGetDiff: () => {},
    onLoadDetail: () => {},
  }));
  assert.match(failedUnknownMarkup, /重试加载详情/,
    "an explicit failed read keeps its retry even without process evidence");

  const incompleteKnownProcessMarkup = renderToStaticMarkup(createElement(
    ProcessTimeline,
    {
      blocks: [], done: true, active: false, engine: "codex",
      deferredCount: 0, openOverride: true,
      detailError: "详细过程未完整返回，请重试",
      onLoadDetail: () => true,
    },
  ));
  assert.match(incompleteKnownProcessMarkup, /加载失败/);
  assert.match(incompleteKnownProcessMarkup, /详细过程未完整返回，请重试/);
  assert.doesNotMatch(incompleteKnownProcessMarkup, /正在加载过程/,
    "a contradictory complete detail becomes retryable instead of spinning");

  const pagedKnownProcessMarkup = renderToStaticMarkup(createElement(ChatView, {
    sid: "paged-known-process-session",
    turns: [{
      id: "paged-known-process-turn", prompt: "inspect", done: true,
      processDetailState: "present", detailReasons: ["process"],
      blocks: [], detailEventCount: 4, detailLoaded: true,
      detailHasMore: true, detailOldestCursor: "older-process-page",
    }],
    engine: "codex", onEdit: () => {}, onGetDiff: () => {},
    onLoadDetail: () => {},
  }));
  assert.doesNotMatch(pagedKnownProcessMarkup,
    /加载失败|详细过程未完整返回/,
    "an empty bounded window must not be mistaken for missing detail");
  assert.match(pagedKnownProcessMarkup, /更多过程/,
    "a bounded empty window retains its real pagination affordance");

  const retryingKnownProcessMarkup = renderToStaticMarkup(createElement(
    ChatView,
    {
      sid: "retrying-known-process-session",
      turns: [{
        id: "retrying-known-process-turn", prompt: "inspect", done: true,
        processDetailState: "present", detailReasons: ["process"],
        blocks: [], detailEventCount: 4, detailLoaded: true,
        detailLoading: true,
      }],
      engine: "codex", onEdit: () => {}, onGetDiff: () => {},
      onLoadDetail: () => {},
    },
  ));
  assert.match(retryingKnownProcessMarkup, /process-spin/);
  assert.doesNotMatch(retryingKnownProcessMarkup,
    /加载失败|详细过程未完整返回/,
    "a retry shows only its loading state until the response settles");

  const runningUnknownMarkup = renderToStaticMarkup(createElement(ChatView, {
    sid: "running-unknown-session",
    turns: [{
      id: "running-unknown-turn", prompt: "wait", done: false,
      processDetailState: "unknown", detailReasons: [],
      blocks: [], detailEventCount: 0, detailLoaded: false,
    }],
    engine: "codex", activeTurnId: "running-unknown-turn",
    onEdit: () => {}, onGetDiff: () => {},
    onLoadDetail: () => {},
  }));
  assert.doesNotMatch(runningUnknownMarkup, /正在处理|已处理|查看本轮详情/);
  assert.match(runningUnknownMarkup, /思考中/,
    "running unknown detail uses only the live tail indicator");

  const runningUntimedProcessMarkup = renderToStaticMarkup(createElement(
    ChatView,
    {
      sid: "running-untimed-process-session",
      turns: [{
        id: "running-untimed-process-turn", prompt: "work", done: false,
        processDetailState: "present", detailReasons: ["process"],
        blocks: [], detailEventCount: 5, detailLoaded: false,
      }],
      engine: "codex", activeTurnId: "running-untimed-process-turn",
      onEdit: () => {}, onGetDiff: () => {},
      onLoadDetail: () => {},
    },
  ));
  assert.match(runningUntimedProcessMarkup, />正在处理</);
  assert.doesNotMatch(runningUntimedProcessMarkup, /正在处理 0s/,
    "a process without a trustworthy first event never invents a timer");

  const zeroTimedProcessMarkup = renderToStaticMarkup(createElement(
    ChatView,
    {
      sid: "zero-timed-process-session",
      turns: [{
        id: "zero-timed-process-turn", prompt: "work", done: true,
        processDetailState: "present", detailReasons: ["process"],
        blocks: [], detailEventCount: 1, detailLoaded: false,
        processStartedTs: 42_000, processDoneTs: 42_000,
      }],
      engine: "codex", onEdit: () => {}, onGetDiff: () => {},
      onLoadDetail: () => {},
    },
  ));
  assert.match(zeroTimedProcessMarkup, />已处理</);
  assert.doesNotMatch(zeroTimedProcessMarkup, /已处理 0s/,
    "equal cached process timestamps keep the label but omit a fake duration");

  const directAnswerMarkup = renderToStaticMarkup(createElement(ChatView, {
    sid: "direct-answer-session", turns: [exactDirectDetail],
    engine: "codex", onEdit: () => {}, onGetDiff: () => {},
    onLoadDetail: () => {},
  }));
  assert.doesNotMatch(directAnswerMarkup,
    /已处理|查看本轮详情|查看完整内容/,
    "an exact direct answer has no process or generic-detail affordance");

  const truncatedAnswerMarkup = renderToStaticMarkup(createElement(ChatView, {
    sid: "truncated-answer-session",
    turns: [{
      id: "truncated-answer-turn", prompt: "long", done: true,
      processDetailState: "none", detailReasons: ["answer_truncated"],
      blocks: [{
        kind: "text", message_id: "truncated-answer-final",
        channel: "final", text: "prefix…", done: true,
      }],
      detailEventCount: 1, detailLoaded: false,
    }],
    engine: "codex", onEdit: () => {}, onGetDiff: () => {},
    onLoadDetail: () => {},
  }));
  assert.doesNotMatch(truncatedAnswerMarkup, /已处理/);
  assert.match(truncatedAnswerMarkup, /查看完整内容/,
    "truncated prose uses a content affordance instead of the process shell");
} finally {
  await harness.close();
}
