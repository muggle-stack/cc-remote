import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { createServer } from "vite";

import type { ServerEvent } from "../src/protocol.ts";
import type { Block, Turn } from "../src/reducer.ts";

const appSource = readFileSync(resolve("src/App.tsx"), "utf8");
assert.match(appSource,
  /if \(msg\.reset_required\)[\s\S]{0,600}historyDetailResetAttemptsRef\.current\.has\(resetKey\)/,
  "an expired detail snapshot may start at most one automatic reset per revision");
assert.match(appSource,
  /historyDetailRequestsRef\.current\.cancelTurn\([\s\S]{0,1800}historyDetailResetAttemptsRef\.current\.add\(resetKey\)/,
  "detail reset revokes stale page waiters before registering the fresh head");
assert.match(appSource,
  /sendGetTurnDetail\(\s*msg\.session_id, msg\.turn_id, target\.revision, null\)/,
  "snapshot recovery requests the authoritative head instead of its cursor");
assert.match(appSource,
  /releaseDetailFailure\(detailTargets\);[\s\S]{0,120}recoverableReads\.retry\(retryKey/,
  "ordinary transient detail failures retain their bounded retry path");

const reducerHarness = await createServer({
  root: process.cwd(),
  appType: "custom",
  logLevel: "silent",
  server: { middlewareMode: true, watch: null },
});

try {
  const { initialState, createRuntime, reduce } =
    await reducerHarness.ssrLoadModule("/src/reducer.ts");
  const event = (body: Record<string, unknown>): ServerEvent => ({
    v: 40,
    ts: 10,
    ...body,
  } as ServerEvent);
  const sid = "detail-reset-session";
  const turnId = "detail-reset-turn";
  const revision = "detail-reset-revision";
  const detailEvents = (toolId: string): ServerEvent[] => [
    event({
      type: "user_msg",
      sid,
      msg_id: turnId,
      prompt: "inspect",
    }),
    event({
      type: "tool_use",
      sid,
      message_id: `${toolId}-message`,
      tool_use_id: toolId,
      tool: "Read",
      input: {},
    }),
    event({
      type: "tool_result",
      sid,
      tool_use_id: toolId,
      content: "ok",
      is_error: false,
    }),
    event({
      type: "turn_end",
      sid,
      turn_id: "native-detail-reset-turn",
      result: {
        subtype: "success",
        duration_ms: 1,
        is_error: false,
      },
    }),
  ];
  const projectionHasTool = (
    turn: Turn | undefined,
    toolId: string,
  ): boolean => !!turn?.detailProjection?.blocks.some((block: Block) =>
    block.kind === "tool" && block.tool_use_id === toolId);

  let state = reduce({ ...initialState, focusedSid: sid }, {
    type: "event",
    event: event({
      type: "history",
      sid,
      session_id: sid,
      revision,
      generation: "detail-reset-generation",
      build_seq: 1,
      live_seq: 0,
      detail: "summary",
      has_more: true,
      oldest_id: turnId,
      newest_id: turnId,
      events: [],
      turns: [{
        id: turnId,
        prompt: "inspect",
        done: true,
        blocks: [{
          kind: "text",
          message_id: "detail-reset-final",
          text: "finished",
          done: true,
          channel: "final",
        }],
        processDetailState: "present",
        detailReasons: ["process"],
        detailEventCount: 4,
        detailLoaded: false,
      }],
    }),
  });
  state = reduce(state, {
    type: "event",
    event: event({
      type: "turn_detail",
      sid,
      session_id: sid,
      turn_id: turnId,
      revision,
      before: null,
      has_more: true,
      oldest_cursor: "td1.old-runtime",
      events: detailEvents("old-runtime-tool"),
    }),
  });
  const oldRuntimeProjection = state.runtimes[sid].turns[0].detailProjection;
  assert.equal(projectionHasTool(
    state.runtimes[sid].turns[0], "old-runtime-tool"), true);

  state = reduce(state, {
    type: "event",
    event: event({
      type: "turn_detail",
      sid,
      session_id: sid,
      turn_id: turnId,
      revision,
      authoritative: false,
      reset_required: true,
      before: "td1.old-runtime",
      error: "详细过程已更新，请重新加载该轮",
      events: [],
    }),
  });
  assert.equal(state.runtimes[sid].turns[0].detailResetPending, true);
  assert.equal(
    state.runtimes[sid].turns[0].detailProjection,
    oldRuntimeProjection,
    "the stale-cursor response keeps expanded runtime detail mounted",
  );
  state = reduce(state, {
    type: "history_detail_reset_requested",
    context: {
      target: "runtime",
      scopeKey: "machine-a:code:codex",
      sid,
      revision,
      turnId,
    },
  });
  assert.equal(state.runtimes[sid].turns[0].detailLoading, true);
  assert.equal(state.runtimes[sid].turns[0].detailProjection,
    oldRuntimeProjection);

  state = reduce(state, {
    type: "event",
    event: event({
      type: "turn_detail",
      sid,
      session_id: sid,
      turn_id: turnId,
      revision,
      before: null,
      events: [event({
        type: "delta",
        sid,
        message_id: "unbound-corrupt-detail",
        text: "invalid",
      })],
    }),
  });
  assert.equal(state.runtimes[sid].turns[0].detailResetPending, true,
    "a malformed fresh head retains replacement semantics for its retry");
  assert.equal(state.runtimes[sid].turns[0].detailProjection,
    oldRuntimeProjection);

  state = reduce(state, {
    type: "event",
    event: event({
      type: "turn_detail",
      sid,
      session_id: sid,
      turn_id: turnId,
      revision,
      before: null,
      has_more: false,
      oldest_cursor: null,
      has_newer: false,
      newer_cursor: null,
      events: detailEvents("fresh-runtime-tool"),
    }),
  });
  let runtimeTurn = state.runtimes[sid].turns[0] as Turn;
  assert.equal(runtimeTurn.detailResetPending, false);
  assert.equal(projectionHasTool(runtimeTurn, "fresh-runtime-tool"), true);
  assert.equal(projectionHasTool(runtimeTurn, "old-runtime-tool"), false,
    "the fresh runtime head atomically replaces stale page segments");

  state = reduce(state, {
    type: "event",
    event: event({
      type: "turn_detail",
      sid,
      session_id: sid,
      turn_id: turnId,
      revision,
      authoritative: false,
      reset_required: true,
      before: "td1.expired-again",
      error: "详细过程已更新，请重新加载该轮",
      events: [],
    }),
  });
  state = reduce(state, {
    type: "history_detail_reset_requested",
    context: {
      target: "runtime",
      scopeKey: "machine-a:code:codex",
      sid,
      revision,
      turnId,
    },
  });
  const beforeEmptyReset = state.runtimes[sid].turns[0].detailProjection;
  state = reduce(state, {
    type: "event",
    event: event({
      type: "turn_detail",
      sid,
      session_id: sid,
      turn_id: turnId,
      revision,
      before: null,
      has_more: false,
      has_newer: false,
      events: [],
    }),
  });
  runtimeTurn = state.runtimes[sid].turns[0] as Turn;
  assert.equal(runtimeTurn.detailResetPending, false,
    "an authoritative empty fresh head settles reset state");
  assert.equal(runtimeTurn.detailProjection, beforeEmptyReset,
    "an empty head cannot revoke earlier positive process evidence");
  assert.equal(runtimeTurn.done, true);
  assert.equal(runtimeTurn.prompt, "inspect");

  state = reduce(state, {
    type: "begin_history_browse",
    sid,
    scopeKey: "machine-a:code:codex",
    revision,
    generation: "detail-reset-generation",
    viewId: "detail-reset-view",
    basePageKey: "detail-reset-head",
  });
  assert.ok(state.historyBrowse);
  state = reduce(state, {
    type: "history_browse_detail",
    sid,
    scopeKey: state.historyBrowse!.scopeKey,
    revision,
    viewId: state.historyBrowse!.viewId,
    windowEpoch: state.historyBrowse!.windowEpoch,
    turnId,
    before: null,
    events: detailEvents("old-browse-tool"),
    hasMore: true,
    oldestCursor: "td1.old-browse",
  });
  const oldBrowseProjection = state.historyBrowse!.turns[0].detailProjection;
  const runtimeBeforeBrowseReset = state.runtimes[sid];
  state = reduce(state, {
    type: "history_browse_detail",
    sid,
    scopeKey: state.historyBrowse!.scopeKey,
    revision,
    viewId: state.historyBrowse!.viewId,
    windowEpoch: state.historyBrowse!.windowEpoch,
    turnId,
    before: "td1.old-browse",
    events: [],
    error: "详细过程已更新，请重新加载该轮",
    resetRequired: true,
  });
  assert.equal(state.historyBrowse!.turns[0].detailResetPending, true);
  assert.equal(state.historyBrowse!.turns[0].detailProjection,
    oldBrowseProjection);
  state = reduce(state, {
    type: "history_detail_reset_requested",
    context: {
      target: "browse",
      scopeKey: state.historyBrowse!.scopeKey,
      sid,
      revision,
      turnId,
      viewId: state.historyBrowse!.viewId,
      windowEpoch: state.historyBrowse!.windowEpoch,
    },
  });
  state = reduce(state, {
    type: "history_browse_detail",
    sid,
    scopeKey: state.historyBrowse!.scopeKey,
    revision,
    viewId: state.historyBrowse!.viewId,
    windowEpoch: state.historyBrowse!.windowEpoch,
    turnId,
    before: null,
    events: detailEvents("fresh-browse-tool"),
    hasMore: false,
    oldestCursor: null,
  });
  const browseTurn = state.historyBrowse!.turns[0] as Turn;
  assert.equal(state.runtimes[sid], runtimeBeforeBrowseReset,
    "browse detail reset cannot write into the live runtime");
  assert.equal(browseTurn.detailResetPending, false);
  assert.equal(projectionHasTool(browseTurn, "fresh-browse-tool"), true);
  assert.equal(projectionHasTool(browseTurn, "old-browse-tool"), false,
    "the fresh browse head atomically replaces stale page segments");

  for (const completed of [false, true]) {
    const sourceId = completed ? "native-completed" : "source-head:thinking";
    const summary: Turn = {
      id: sourceId, prompt: "", blocks: [], done: completed,
      ts: 100_000, detailLoaded: false, processDetailState: "present",
      detailEventCount: 4,
    };
    const previous: Turn = {
      id: "previous-user", prompt: "previous request", done: true, blocks: [],
    };
    let detailState = {
      ...initialState, focusedSid: sid,
      runtimes: { [sid]: {
        ...createRuntime(), state: completed ? "idle" : "running",
        historyRevision: revision, historyGeneration: "source-generation",
        hasMore: true, oldestId: previous.id,
        turns: [previous, summary],
      } },
    };
    const page = (toolId: string): ServerEvent[] => [
      event({ type: "turn_binding", sid, ts: 100,
        msg_id: sourceId, turn_id: sourceId, autonomous: true }),
      // The first assistant item belongs to another page. This page's own
      // first item must not become the identity of the complete source turn.
      event({ type: "assistant_msg_start", sid, ts: 102,
        message_id: `${toolId}-message`, channel: "thinking" }),
      event({ type: "delta", sid, ts: 102,
        message_id: `${toolId}-message`, channel: "thinking", text: "inspect" }),
      event({ type: "assistant_msg_end", sid, ts: 103,
        message_id: `${toolId}-message`, channel: "thinking" }),
      ...detailEvents(toolId).filter(e => e.type === "tool_use" || e.type === "tool_result"),
      ...(completed ? [event({ type: "turn_end", sid, ts: 110,
        turn_id: sourceId,
        result: { subtype: "success", duration_ms: 10000, is_error: false } })] : []),
    ];
    detailState = reduce(detailState, {
      type: "event", event: event({
        type: "turn_detail", sid, session_id: sid, turn_id: sourceId,
        revision, authoritative: true, events: page("latest-detail"),
        before: null, has_more: true, oldest_cursor: "older-source-page",
      }),
    });
    assert.equal(detailState.runtimes[sid].turns.length, 2);
    let sourceTurn = detailState.runtimes[sid].turns[1] as Turn;
    assert.equal(sourceTurn.id, sourceId);
    assert.equal(sourceTurn.detailError, undefined);
    assert.equal(sourceTurn.done, completed);
    assert.equal(sourceTurn.ts, 100_000);
    assert.equal(sourceTurn.prompt, "");
    assert.equal(projectionHasTool(sourceTurn, "latest-detail"), true);
    assert.equal(detailState.runtimes[sid].turns[0], previous,
      "detail for an assistant-only turn cannot change its previous human turn");
    detailState = reduce(detailState, {
      type: "event", event: event({
        type: "turn_detail", sid, session_id: sid, turn_id: sourceId,
        revision, authoritative: true, events: page("older-detail"),
        before: "older-source-page", has_more: false, has_newer: true,
        newer_cursor: "latest-source-page",
      }),
    });
    sourceTurn = detailState.runtimes[sid].turns[1] as Turn;
    assert.equal(sourceTurn.detailError, undefined);
    assert.equal(projectionHasTool(sourceTurn, "latest-detail"), true);
    assert.equal(projectionHasTool(sourceTurn, "older-detail"), true);
    detailState = reduce(detailState, {
      type: "begin_history_browse", sid, scopeKey: "machine:code:claude",
      revision, generation: "source-generation", viewId: "assistant-only-view",
      basePageKey: "assistant-only-head",
    });
    assert.ok(detailState.historyBrowse);
    const runtimeBefore = detailState.runtimes[sid];
    detailState = reduce(detailState, {
      type: "history_browse_detail", sid, scopeKey: detailState.historyBrowse.scopeKey,
      revision, viewId: detailState.historyBrowse.viewId,
      windowEpoch: detailState.historyBrowse.windowEpoch, turnId: sourceId,
      events: page("browse-detail"), before: null, hasMore: false,
    });
    const browsed = detailState.historyBrowse.turns.find((t: Turn) => t.id === sourceId);
    assert.equal(browsed?.detailError, undefined);
    assert.equal(browsed?.done, completed);
    assert.equal(projectionHasTool(browsed, "browse-detail"), true);
    assert.equal(detailState.runtimes[sid], runtimeBefore);
  }

  {
    const visibleId = "completed-after-partial-detail";
    const nativeId = "native-completed-after-partial-detail";
    const messageId = "same-native-answer";
    const prefix = "根因是 Claude 压缩后的部分过程没有";
    const fullText = prefix + "用户消息作为锚点，分页后丢失轮次标识。";
    const summary: Turn = {
      id: visibleId, forkPointId: nativeId, prompt: "检查显示", done: false,
      blocks: [{ kind: "text", message_id: messageId,
        channel: "final", text: prefix, done: false }],
      detailLoaded: false, processDetailState: "present", detailEventCount: 4,
    };
    let state = {
      ...initialState, engine: "codex", focusedSid: sid,
      sessions: [{ session_id: sid, engine: "codex", space: "code" }],
      runtimes: { [sid]: {
        ...createRuntime(), state: "running", historyRevision: revision,
        turns: [summary],
      } },
    };
    state = reduce(state, { type: "event", event: event({
      type: "turn_detail", sid, session_id: sid, turn_id: visibleId,
      revision, authoritative: true, has_more: false, has_newer: false,
      events: [
        event({ type: "user_msg", sid, msg_id: visibleId, prompt: summary.prompt }),
        event({ type: "delta", sid, message_id: messageId, channel: "final", text: prefix }),
        event({ type: "assistant_msg_end", sid, message_id: messageId, channel: "final" }),
      ],
    }) });
    const correction = event({ type: "delta", sid, turn_id: nativeId,
      message_id: messageId, channel: "final", text: fullText, replace: true });
    const corrected = reduce(state, { type: "event", event: correction });
    assert.equal(corrected.runtimes[sid].turns[0].blocks
      .find((block: Block) => block.kind === "text")?.text, fullText,
    "a completion snapshot repairs a closed prefix while its Codex task is still active");
    state = reduce(state, { type: "event", event: event({
      type: "turn_end", sid, turn_id: nativeId,
      result: { subtype: "success", duration_ms: 1000, is_error: false },
    }) });
    state = reduce(state, { type: "event", event: event({ type: "state", sid, state: "idle" }) });
    state = reduce(state, { type: "event", event: event({
      type: "history", sid, session_id: sid, revision, detail: "summary",
      in_progress: false, newest_id: visibleId, events: [], has_more: false,
      turns: [{ ...summary, done: true, blocks: [{ ...summary.blocks[0], text: fullText, done: true }] }],
    }) });
    assert.equal(state.runtimes[sid].turns.length, 1);
    const repaired = state.runtimes[sid].turns[0];
    assert.equal(repaired.blocks.find((block: Block) => block.kind === "text")?.text, fullText,
      "completed canonical history repairs text cached from an in-flight detail read");
    assert.equal(repaired.done, true);
    const late = reduce(state, { type: "event", event: {
      ...correction, text: "late stale replacement",
    } });
    assert.equal(late.runtimes[sid].turns[0].blocks
      .find((block: Block) => block.kind === "text")?.text, fullText,
    "a delayed snapshot cannot replace the body after the task has finished");
    const shortened = { ...summary, done: true, detailReasons: ["answer_truncated"],
      blocks: [{ ...summary.blocks[0], text: prefix + "…", done: true }] };
    state = reduce(state, { type: "event", event: event({
      type: "history", sid, session_id: sid, revision, detail: "summary",
      in_progress: false, newest_id: visibleId, events: [], has_more: false,
      turns: [shortened],
    }) });
    assert.equal(state.runtimes[sid].turns[0].blocks
      .find((block: Block) => block.kind === "text")?.text, fullText,
    "an explicitly shortened summary still preserves the complete loaded answer");
  }
} finally {
  await reducerHarness.close();
}
