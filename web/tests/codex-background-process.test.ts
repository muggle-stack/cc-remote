import assert from "node:assert/strict";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";
import type { Block, ProcessBlock, TextBlock } from "../src/domain/conversation.ts";
import { PROTOCOL_VERSION, type ServerEvent } from "../src/protocol.ts";
import { mergeDetailWithLiveTail } from "../src/history-merge.ts";

const harness = await createServer({ root: process.cwd(), appType: "custom",
  logLevel: "silent", server: { middlewareMode: true, watch: null } });
try {
  const { assistantContinuations } = await harness.ssrLoadModule("/src/assistant-continuations.ts") as
    typeof import("../src/assistant-continuations.ts");
  const codexTaskContinuations = (blocks: Block[], answers: TextBlock[]) =>
    assistantContinuations(blocks, answers, "codex");

  const answer = (id: string, text: string): TextBlock => ({
    kind: "text", message_id: id, text, channel: "final", done: true,
  });
  const receipt = (id: string): ProcessBlock => ({
    kind: "process", item_id: id, processKind: "task", phase: "end",
    status: "succeeded", title: "Background verification", summary: "任务已完成",
    server: "cc_remote_tasks", tool: "task_result", done: true, startedTs: 20_000,
  });
  const before = answer("before", "Before callback.");
  const after = answer("after", "After callback.");
  const later = answer("later", "Second callback.");
  const thinking: ProcessBlock = {
    kind: "process", item_id: "thinking", processKind: "reasoning",
    phase: "end", status: "succeeded", title: "思考", summary: "Check result", done: true,
  };
  const blocks: Block[] = [before, receipt("callback-1"), thinking, after,
    receipt("callback-2"), later];
  const answers = [before, after, later];
  const split = codexTaskContinuations(blocks, answers);
  assert.deepEqual(split.answers, [before]);
  assert.deepEqual(split.original, [before]);
  assert.deepEqual(split.continuations.map(segment => segment.answers), [[after], [later]]);
  assert.equal(split.continuations[0]?.startedTs, 20_000);
  assert.equal(codexTaskContinuations([receipt("a"), receipt("b"), after], [after])
    .continuations.length, 1, "coalesced receipts share one continuation boundary");
  assert.equal(codexTaskContinuations([answer("normal", "I received a background message.")], [])
    .continuations.length, 0, "assistant prose cannot fabricate a native task receipt");
  assert.equal(codexTaskContinuations([{ ...receipt("foreign"), server: "other" }], [])
    .continuations.length, 0);

  const merged = mergeDetailWithLiveTail(
    [before, { ...receipt("callback-1"), detail: "Actual result" }, thinking, after],
    [before, receipt("callback-1"), after], true,
  );
  assert.equal(codexTaskContinuations(merged, [before, after]).continuations.length, 1,
    "expanding history detail cannot duplicate the compact receipt boundary");

  const { ChatView } = await harness.ssrLoadModule("/src/components/ChatView.tsx");
  const { default: BackgroundTaskControl } = await harness.ssrLoadModule(
    "/src/components/BackgroundTaskControl.tsx");
  const { initialState, createRuntime, reduce } = await harness.ssrLoadModule("/src/reducer.ts");
  const renderChat = (items: Block[]) => renderToStaticMarkup(createElement(ChatView, {
    sid: "codex-session", engine: "codex", turns: [{ id: "turn", prompt: "Run check",
      done: true, blocks: items, processDetailState: "present", detailEventCount: 3 }],
  }));
  const markup = renderChat(blocks);
  assert.match(markup, /Before callback\.[\s\S]*Codex 收到后台消息，继续处理[\s\S]*After callback\./);
  assert.equal(markup.split("Codex 收到后台消息，继续处理").length - 1, 2);
  assert.doesNotMatch(markup, /Claude 继续处理/);
  assert.equal(markup.split("After callback.").length - 1, 1);
  const restored = renderChat([before, receipt("callback-1"), after]);
  assert.match(restored, /Before callback\.[\s\S]*Codex 收到后台消息，继续处理[\s\S]*After callback\./,
    "compact history shows the source label before process details load");

  const sid = "codex-session", otherSid = "another-account-session";
  let state = { ...initialState, focusedSid: sid, runtimes: {
    [sid]: { ...createRuntime(), state: "idle", controlGeneration: "generation" },
    [otherSid]: { ...createRuntime(), state: "idle", controlGeneration: "generation" },
  } };
  const send = (body: Record<string, unknown>) => {
    state = reduce(state, { type: "event", event: {
      v: PROTOCOL_VERSION, ts: 20, sid, ...body,
    } as ServerEvent });
  };
  const items = [{ item_id: "async-task:one", kind: "task", status: "running",
    title: "Background verification", summary: "后台执行中", started_at: 10, updated_at: 11 }];
  send({ type: "background_process_sync", generation: "generation", items });
  assert.equal(state.runtimes[sid].state, "idle", "background execution is not a busy main turn");
  assert.equal(state.runtimes[sid].backgroundProcesses.length, 1);
  assert.equal(state.runtimes[otherSid].backgroundProcesses.length, 0);
  const dock = renderToStaticMarkup(createElement(BackgroundTaskControl, {
    processes: state.runtimes[sid].backgroundProcesses,
  }));
  assert.match(dock, /后台任务 · 1/);
  assert.match(dock, /aria-haspopup="dialog"/);
  send({ type: "background_process_sync", generation: "old-generation", items: [] });
  assert.equal(state.runtimes[sid].backgroundProcesses.length, 1,
    "an old wrapper snapshot cannot clear the current task");
  send({ type: "user_msg", msg_id: "human-turn", prompt: "another question" });
  send({ type: "turn_end", turn_id: "human-turn", result: { status: "completed" } });
  assert.equal(state.runtimes[sid].backgroundProcesses.length, 1,
    "finishing a main response does not finish the detached task");
  send({ type: "process", item_id: "live-receipt", kind: "task", phase: "end",
    status: "succeeded", title: "Background verification", turn_id: "callback-turn",
    server: "cc_remote_tasks", tool: "task_result" });
  const projected = state.runtimes[sid].turns.flatMap((turn: { blocks: Block[] }) => turn.blocks)
    .find((block: Block) => block.kind === "process" && block.item_id === "live-receipt");
  assert.equal(projected?.startedTs, 20_000);
  send({ type: "background_process_sync", generation: "generation", items: [] });
  assert.equal(state.runtimes[sid].backgroundProcesses.length, 0);

  state = { ...initialState, focusedSid: sid, runtimes: { [sid]: createRuntime() } };
  send({ type: "background_process_sync", generation: "generation", items });
  assert.equal(state.runtimes[sid].backgroundProcesses.length, 1,
    "a fresh page restores ongoing work without replaying task-start tools");
  console.log("Codex background tasks: activity, narrative order, replay and rendering passed");
} finally {
  await harness.close();
}
