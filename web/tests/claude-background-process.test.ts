import assert from "node:assert/strict";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

import type { Block } from "../src/domain/conversation.ts";
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
    /Build is running\.[\s\S]*Build completed[\s\S]*Claude 随后继续回复[\s\S]*Build passed\./,
    "a task-completion follow-up keeps its source-time narrative boundary",
  );

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
    /Task A completed[\s\S]*A report\.[\s\S]*Task B completed[\s\S]*B report\./,
    "each detached completion labels its own later reply segment",
  );
} finally {
  await harness.close();
}
