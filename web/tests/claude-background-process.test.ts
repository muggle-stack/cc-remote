import assert from "node:assert/strict";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

import type { Block } from "../src/domain/conversation.ts";
import { isComposerBusy } from "../src/composer-submit.ts";
import {
  MAX_BACKGROUND_PROCESS_ITEMS,
  PROTOCOL_VERSION,
  type ServerEvent,
} from "../src/protocol.ts";


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
  const { claudeContinuations } = await harness.ssrLoadModule("/src/claude-continuations.ts");
  const event = (body: Record<string, unknown>): ServerEvent => ({
    v: PROTOCOL_VERSION,
    ts: 10,
    ...body,
  } as ServerEvent);

  const sid = "claude-background-sync";
  let state = {
    ...initialState,
    focusedSid: sid,
    runtimes: {
      [sid]: { ...createRuntime(), controlGeneration: "generation-1" },
    },
  };
  state = reduce(state, { type: "event", event: event({
    type: "background_process_sync",
    sid,
    generation: "generation-1",
    items: [{
      item_id: "bash-task",
      kind: "task",
      status: "running",
      title: "Run verification",
      command: "make verify",
      started_at: 12,
      updated_at: 13,
    }],
  }) });
  assert.equal(state.runtimes[sid].backgroundProcesses[0]?.command,
    "make verify");
  assert.equal(state.runtimes[sid].backgroundProcesses[0]?.startedTs,
    12_000);

  const stale = reduce(state, { type: "event", event: event({
    type: "background_process_sync",
    sid,
    generation: "generation-old",
    items: [],
  }) });
  assert.equal(stale.runtimes[sid].backgroundProcesses.length, 1,
    "a delayed old-generation level cannot clear current work");

  state = {
    ...state,
    runtimes: {
      ...state.runtimes,
      [sid]: {
        ...state.runtimes[sid],
        historyRevision: "background-r1",
        turns: [{
          id: "turn-1",
          prompt: "run verification",
          done: false,
          processDetailState: "present",
          processStartedTs: 12_000,
          blocks: [{
            kind: "process",
            item_id: "bash-task",
            processKind: "task",
            phase: "start",
            status: "running",
            title: "Run verification",
            background: true,
            done: false,
            startedTs: 12_000,
          }],
        }],
      },
    },
  };
  state = reduce(state, { type: "event", event: event({
    type: "background_process_sync",
    sid,
    generation: "generation-1",
    items: [],
  }) });
  assert.equal(state.runtimes[sid].backgroundProcesses.length, 0,
    "the authoritative empty level clears a stale task card");
  const settled = state.runtimes[sid].turns[0].blocks[0];
  assert.equal(settled.kind, "process");
  assert.equal(settled.done, true,
    "the same empty level settles a stale in-turn spinner");
  assert.equal(settled.kind === "process" && settled.status, "unknown",
    "absence proves terminality but must not fabricate success");

  state = reduce(state, { type: "event", event: event({
    type: "turn_detail",
    session_id: sid,
    turn_id: "turn-1",
    revision: "background-r1",
    events: [
      event({ type: "user_msg", sid, msg_id: "turn-1",
        prompt: "run verification" }),
      event({ type: "process", sid, item_id: "late-detail-task",
        kind: "task", phase: "start", status: "running",
        title: "Late cached task", background: true }),
    ],
    has_more: false,
    has_newer: false,
  }) });
  const detailProcess = state.runtimes[sid].turns[0]
    .detailProjection?.blocks.find((block: Block) => block.kind === "process");
  assert.equal(detailProcess?.done, true,
    "detail arriving after the empty level cannot resurrect a ghost task");
  assert.equal(detailProcess?.kind === "process" && detailProcess.status,
    "unknown");

  const cappedSid = "claude-background-cap";
  let cappedState = {
    ...initialState,
    focusedSid: cappedSid,
    runtimes: { [cappedSid]: createRuntime() },
  };
  for (let index = 0; index < MAX_BACKGROUND_PROCESS_ITEMS + 12; index += 1) {
    cappedState = reduce(cappedState, { type: "event", event: event({
      type: "process", sid: cappedSid, item_id: `task-${index}`,
      kind: "task", phase: "start", status: "running",
      title: `Task ${index}`, background: true,
    }) });
  }
  assert.equal(
    cappedState.runtimes[cappedSid].backgroundProcesses.length,
    MAX_BACKGROUND_PROCESS_ITEMS,
    "untrusted edge volume cannot grow the detached-task dock past protocol bounds",
  );

  const followupMarkup = renderToStaticMarkup(createElement(ChatView, {
    sid: "followup-history",
    engine: "claude",
    turns: [{
      id: "turn-1",
      prompt: "run",
      done: true,
      doneTs: 21_000,
      blocks: [
        {
          kind: "text",
          message_id: "before",
          channel: "final",
          text: "Build is running.",
          done: true,
          startedTs: 2_000,
        },
        {
          kind: "process",
          item_id: "task-1",
          processKind: "task",
          phase: "end",
          status: "succeeded",
          title: "Build",
          summary: "Build completed",
          background: true,
          done: true,
          terminalTs: 20_000,
        },
        {
          kind: "text",
          message_id: "after",
          channel: "final",
          text: "Build passed.",
          done: true,
          background: true,
          startedTs: 21_000,
        },
      ],
    }],
  }));
  assert.match(
    followupMarkup,
    /Build is running\.[\s\S]*Claude 继续处理[\s\S]*Build passed\./,
    "a task-completion follow-up keeps its source-time narrative boundary",
  );

  for (const detailState of [
    { detailLoaded: false },
    { detailLoaded: false, detailLoading: true },
    { detailLoaded: false, detailError: "detail unavailable" },
    { detailLoaded: true, detailHasMore: true, detailOldestCursor: "older" },
  ]) {
    const finalOnlyMarkup = renderToStaticMarkup(createElement(ChatView, {
      sid: "final-only-continuation", engine: "claude",
      turns: [{
        id: "native-turn", prompt: "run", done: true,
        ts: 1_000, doneTs: 21_000, processDetailState: "present",
        detailEventCount: 28, ...detailState,
        blocks: [
          { kind: "text", message_id: "original", channel: "final",
            text: "Original response.", done: true, startedTs: 2_000 },
          { kind: "text", message_id: "continued-final", channel: "final",
            text: "Completed follow-up.", done: true, background: true,
            startedTs: 20_000, doneTs: 21_000 },
        ],
      }],
    }));
    const continuation = finalOnlyMarkup.slice(finalOnlyMarkup.indexOf("Claude 继续处理"));
    assert.match(continuation, /Completed follow-up\./);
    assert.doesNotMatch(continuation, /已处理|正在处理|process-timeline/,
      "parent detail counts/loading/errors must not create an empty continuation disclosure");
    assert.equal(finalOnlyMarkup.split("Completed follow-up.").length - 1, 1,
      "the real answer remains visible exactly once");
  }

  const processFollowupMarkup = renderToStaticMarkup(createElement(ChatView, {
    sid: "followup-with-process", engine: "claude",
    turns: [{ id: "native-turn", prompt: "run", done: true,
      processDetailState: "present", detailEventCount: 28,
      blocks: [
        { kind: "text", message_id: "original", channel: "final",
          text: "Original response.", done: true },
        { kind: "text", message_id: "continued-thought", channel: "thinking",
          text: "Reviewing the background result.", done: true, background: true },
        { kind: "text", message_id: "continued-final", channel: "final",
          text: "Completed follow-up.", done: true, background: true },
      ],
    }],
  }));
  assert.match(processFollowupMarkup,
    /Claude 继续处理[\s\S]*已处理[\s\S]*1 项[\s\S]*Completed follow-up\./,
    "a continuation with an actual process item retains its own disclosure");

  const concurrentFollowupMarkup = renderToStaticMarkup(createElement(ChatView, {
    sid: "concurrent-followup-history",
    engine: "claude",
    turns: [{
      id: "turn-concurrent",
      prompt: "run both",
      done: true,
      blocks: [
        { kind: "process", item_id: "task-a", processKind: "task",
          phase: "end", status: "succeeded", title: "Task A",
          summary: "Task A completed", background: true, done: true,
          terminalTs: 20_000 },
        { kind: "text", message_id: "reply-a", channel: "final",
          text: "A report.", done: true, background: true,
          startedTs: 21_000 },
        { kind: "process", item_id: "task-b", processKind: "agent",
          phase: "end", status: "succeeded", title: "Task B",
          summary: "Task B completed", background: true, done: true,
          terminalTs: 30_000 },
        { kind: "text", message_id: "reply-b", channel: "final",
          text: "B report.", done: true, background: true,
          startedTs: 31_000 },
      ],
    }],
  }));
  assert.match(
    concurrentFollowupMarkup,
    /Claude 继续处理[\s\S]*A report\.[\s\S]*Claude 继续处理[\s\S]*B report\./,
    "each detached completion labels its own later reply segment",
  );

  const blocks: Block[] = [
    { kind: "text", message_id: "original", channel: "final", text: "Interim", done: true },
    { kind: "process", item_id: "child", processKind: "agent", phase: "end",
      title: "Long private report stays in child details", status: "succeeded", done: true, background: true },
    { kind: "text", message_id: "thinking-a", channel: "thinking", text: "Fixture thought A", done: true, background: true, startedTs: 20_000 },
    { kind: "tool", message_id: "tool-a-message", tool_use_id: "tool-a", tool: "Bash", input: {}, done: true, background: true },
    { kind: "text", message_id: "answer-a", channel: "final", text: "Result A", done: true, background: true },
    { kind: "text", message_id: "thinking-b", channel: "thinking", text: "", done: false, background: true, startedTs: 30_000 },
  ];
  const narrative = claudeContinuations(blocks, blocks.filter((block) => block.kind === "text" && block.channel === "final"));
  assert.equal(narrative.original.length, 2);
  assert.equal(narrative.continuations.length, 2);
  assert.equal(narrative.continuations[0].blocks.length, 3);
  assert.equal(narrative.continuations[0].answers[0].message_id, "answer-a");
  assert.equal(narrative.continuations[1].id, "thinking-b",
    "a main continuation is visible from its first empty stream frame");

  let continuationState = {
    ...initialState, focusedSid: sid,
    runtimes: { [sid]: { ...createRuntime(), controlGeneration: "generation-1",
      turns: [{ id: "settled-parent", prompt: "run", done: true, blocks: [] }] } },
  };
  const send = (body: Record<string, unknown>) => {
    continuationState = reduce(continuationState, { type: "event", event: event({ sid, ...body }) });
  };
  send({ type: "background_process_sync", generation: "generation-1", items: [{
    item_id: "other-child", kind: "task", status: "running", title: "Background check",
    started_at: 12, updated_at: 13,
  }] });
  assert.equal(isComposerBusy(continuationState.runtimes[sid].state), false,
    "a background child alone must leave the composer available for a normal query");
  send({ type: "state", state: "running" });
  send({ type: "process", turn_id: "settled-parent", item_id: "child", kind: "agent",
    phase: "end", status: "succeeded", title: "Child done", background: true });
  assert.equal(continuationState.runtimes[sid].liveOwner, null,
    "a completed child cannot claim the parent's running owner");
  send({ type: "assistant_msg_start", turn_id: "settled-parent", message_id: "continuation", channel: "thinking", background: true });
  assert.equal(continuationState.runtimes[sid].liveOwner?.turnId, "settled-parent");
  assert.equal(continuationState.runtimes[sid].turns[0].done, true,
    "the prior native completion receipt remains settled");
  send({ type: "state", state: "idle" });
  assert.equal(isComposerBusy(continuationState.runtimes[sid].state), false,
    "the real main terminal restores normal submission without a hard refresh");
  assert.equal(continuationState.runtimes[sid].backgroundProcesses[0]?.status, "running",
    "settling the main response keeps the still-running child visible");
  send({ type: "delta", turn_id: "settled-parent", message_id: "continuation", channel: "thinking", text: "Late replay", background: true });
  assert.equal(continuationState.runtimes[sid].liveOwner, null,
    "replay into an idle session cannot revive the spark");
  send({ type: "state", state: "running", msg_id: "settled-parent", continuation: true, seq: 20 });
  assert.equal(isComposerBusy(continuationState.runtimes[sid].state), true,
    "a later native continuation, rather than the child job, owns steering mode");
  assert.equal(continuationState.runtimes[sid].liveOwner?.turnId, "settled-parent",
    "the native continuation owns its spark before any text or tool output");
  assert.equal(continuationState.runtimes[sid].turns[0].done, true);
  send({ type: "state", state: "idle", seq: 21 });
  assert.equal(continuationState.runtimes[sid].liveOwner, null);
  assert.equal(isComposerBusy(continuationState.runtimes[sid].state), false);
} finally {
  await harness.close();
}
