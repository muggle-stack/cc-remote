import assert from "node:assert/strict";
import { resolve } from "node:path";
import { createServer } from "vite";
import { PROTOCOL_VERSION } from "../src/protocol.ts";
import type { Turn } from "../src/reducer.ts";

const harness = await createServer({ root: process.cwd(), appType: "custom",
  logLevel: "silent", server: { middlewareMode: true, watch: null } });
try {
  const { producedFiles } = await harness.ssrLoadModule(resolve("../integrations/dsh/cc-remote.mjs"));
  const patchEvents = (text: string, error = false) => [
    { type: "tool/call", data: { callId: "patch", name: "apply_patch", arguments: JSON.stringify({ input: ["created.txt", "moved.txt", "removed.txt", "saved.txt", "forged.txt", "never.txt"].map(path => "*** Add File: " + path).join("\n") }) } },
    { type: "tool/result", data: { message: { toolCallId: "patch", isError: error, content: [{ type: "text", text }] } } },
  ];
  assert.deepEqual(producedFiles(patchEvents("Success. Updated the following files:\nA created.txt\nM moved.txt\nD removed.txt")).map((f: { path: string }) => f.path).sort(), ["created.txt", "moved.txt"]);
  assert.deepEqual(producedFiles(patchEvents("Patch stopped after 1 of 3 operations.\nCompleted:\nA saved.txt\nFailed:\nA forged.txt\nNot executed:\nA never.txt")).map((f: { path: string }) => f.path), ["saved.txt"]);
  assert.deepEqual(producedFiles(patchEvents("A never.txt", true)), []);
  assert.deepEqual(producedFiles(patchEvents("Success. Updated the following files:\nA unrequested.txt")), []);
  assert.deepEqual(producedFiles(patchEvents("Success. Updated the following files:\nA /project/created.txt"), "/project").map((f: { path: string }) => f.path), ["/project/created.txt"]);
  assert.deepEqual(producedFiles(patchEvents("Success. Updated the following files:\nA /other/created.txt"), "/project"), []);
  const { routeComposerInput, commandsFor, defaultModelFor } = await harness.ssrLoadModule("/src/data.ts");
  const { dshCommandMatches } = await harness.ssrLoadModule("/src/dsh-commands.ts");
  for (const input of ["/btw", "/btw question", " /BTW question "]) {
    assert.equal(routeComposerInput(input, "dsh").kind, "blocked");
    for (const engine of ["claude", "codex"]) {
      assert.equal(routeComposerInput(input, engine).kind, "client");
    }
  }
  assert.equal(commandsFor("dsh").some((command: { slash?: string }) => command.slash === "btw"), false);
  assert.deepEqual(dshCommandMatches("btw", { commands: [{ name: "btw", description: "old command" }] }), []);
  const { initialState, createRuntime, reduce } = await harness.ssrLoadModule("/src/reducer.ts");
  const { shortSessionId } = await harness.ssrLoadModule("/src/session-list.ts");
  assert.equal(shortSessionId("dsh", "session-c4edc56f-aea8-4b43-bda9-0eac124c5559"), "c4edc56f");
  assert.equal(shortSessionId("dsh", "c4edc56f-aea8-4b43-bda9-0eac124c5559"), "c4edc56f");
  assert.equal(shortSessionId("codex", "019ece7f-dd64-7cb1-9527-5d676b6b3a39"), "019ece7f");
  assert.equal(shortSessionId("claude", "3aba1ed1-588d-4e01-bfba-56cfd3dd01c5"), "3aba1ed1");
  const { canForkTurn } = await harness.ssrLoadModule("/src/session-worktree.ts");
  const { mergeAuthoritativeTurnDetail } = await harness.ssrLoadModule("/src/history-merge.ts");
  const { turnNotificationPresentation } = await harness.ssrLoadModule("/src/turn-notification.ts");
  const { skillCatalogKey, skillCatalogResponseMatches } = await harness.ssrLoadModule("/src/skill-catalog-cache.ts");
  const catalogKey = (sid: string) => skillCatalogKey("machine", "dsh", "code", "/repo", null, null, sid);
  assert.notEqual(catalogKey("dsh@one"), catalogKey("dsh@two"), "native presets differ between sessions sharing a cwd");
  assert.notEqual(catalogKey("preset:standard"), catalogKey("preset:minimal"));
  const draftRead = { requestId: "draft", engine: "dsh", space: "code", cwd: "/repo", skillsOnly: true };
  const draftResponse = { request_id: "draft", engine: "dsh", space: "code", cwd: "/repo", skills_only: true };
  assert.ok(skillCatalogResponseMatches(draftRead, draftResponse));
  assert.equal(skillCatalogResponseMatches(draftRead, { ...draftResponse, sid: "dsh@one" }), false);
  assert.deepEqual(turnNotificationPresentation({ sid: "dsh@session",
    result: { subtype: "success", is_error: false },
    notification_context: { engine: "dsh", space: "code" } }, "session"), {
    title: "DSH 会话", body: "DSH 会话已经完成", sessionId: "dsh@session", engine: "dsh", space: "code",
  });
  const sid = "dsh@session";
  let state = { ...initialState, focusedSid: sid, runtimes: {
    [sid]: { ...createRuntime(), state: "running", syncReady: true },
    "dsh@other": createRuntime(),
  } };
  let seq = 0;
  const emit = (body: Record<string, unknown>) => {
    state = reduce(state, { type: "event", event: {
      v: PROTOCOL_VERSION, sid, ts: 1, seq: ++seq, ...body,
    } });
  };
  emit({ type: "user_msg", msg_id: "first", client_msg_id: "first", prompt: "first" });
  emit({ type: "turn_binding", msg_id: "first", turn_id: "dsh-seq-2" });
  const text = (turn: string, message: string, value: string, replace = false) => {
    emit({ type: "assistant_msg_start", turn_id: turn, message_id: message });
    emit({ type: "delta", turn_id: turn, message_id: message, text: value, replace });
  };
  text("dsh-seq-2", "first-answer", "discarded attempt");
  text("dsh-seq-2", "first-answer", "", true);
  text("dsh-seq-2", "first-answer", "old answer");
  emit({ type: "user_msg", msg_id: "steer", client_msg_id: "steer", prompt: "steer" });
  emit({ type: "turn_binding", msg_id: "steer", turn_id: "dsh-seq-8" });
  text("dsh-seq-2", "first-answer", "old answer complete", true);
  emit({ type: "assistant_msg_end", turn_id: "dsh-seq-2", message_id: "first-answer" });
  emit({ type: "turn_end", turn_id: "dsh-seq-2", result: { subtype: "steered", duration_ms: 0, is_error: false } });
  text("dsh-seq-8", "steer-answer", "new answer", true);
  emit({ type: "assistant_msg_end", turn_id: "dsh-seq-8", message_id: "steer-answer" });
  emit({ type: "turn_end", turn_id: "dsh-seq-8", result: { subtype: "success", duration_ms: 5, is_error: false } });
  emit({ type: "turn_binding", msg_id: "dsh-turn-2", turn_id: "dsh-turn-2", autonomous: true });
  text("dsh-turn-2", "auto-answer", "goal continuation", true);
  emit({ type: "assistant_msg_end", turn_id: "dsh-turn-2", message_id: "auto-answer" });
  emit({ type: "turn_end", turn_id: "dsh-turn-2", result: { subtype: "success", duration_ms: 5, is_error: false } });
  const rows = state.runtimes[sid].turns as Turn[];
  assert.deepEqual(rows.map(row => row.id), ["first", "steer", "dsh-turn-2"]);
  assert.deepEqual(rows.map(row => row.blocks.filter(b => b.kind === "text").map(b => b.text)),
    [["old answer complete"], ["new answer"], ["goal continuation"]]);
  assert.ok(rows.every(row => row.done));
  assert.deepEqual(rows.map(row => row.continuation), [undefined, undefined, undefined]);
  assert.deepEqual(rows.map(row => canForkTurn("dsh", row)), [false, true, true]);
  assert.equal(rows[0].forkPointId, "dsh-seq-2", "keep the native identity for late event routing");
  const canonicalOld = { ...rows[0], forkPointId: undefined, forkAvailable: undefined };
  assert.equal(canForkTurn("dsh", mergeAuthoritativeTurnDetail(canonicalOld, rows[0])), false,
    "refreshing a completed steer segment must not restore its fork button");
  emit({ type: "turn_binding", msg_id: "dsh-turn-3", turn_id: "dsh-turn-3", autonomous: true });
  emit({ type: "turn_binding", msg_id: "dsh-turn-3", turn_id: "dsh-turn-3", autonomous: true, continuation: "subagent" });
  text("dsh-turn-3", "child-answer", "child result checked", true);
  emit({ type: "assistant_msg_end", turn_id: "dsh-turn-3", message_id: "child-answer" });
  emit({ type: "turn_end", turn_id: "dsh-turn-3", result: { subtype: "success", duration_ms: 5, is_error: false } });
  assert.equal(state.runtimes[sid].turns.length, 4, "late wakeup metadata updates the same native row");
  assert.equal(state.runtimes[sid].turns.at(-1).continuation, "subagent");
  emit({ type: "user_msg", msg_id: "next", client_msg_id: "next", prompt: "new task" });
  emit({ type: "turn_binding", msg_id: "next", turn_id: "dsh-seq-30" });
  emit({ type: "turn_end", turn_id: "dsh-seq-30", result: { subtype: "success", duration_ms: 1, is_error: false } });
  assert.equal(state.runtimes[sid].turns.at(-1).continuation, undefined);
  const summaries = state.runtimes[sid].turns.map((turn: Turn) => ({
    id: turn.id, prompt: turn.prompt, blocks: turn.blocks, done: turn.done,
    forkPointId: turn.forkPointId, continuation: turn.continuation,
    detailEventCount: 0, detailLoaded: false,
  }));
  const cold = reduce({ ...initialState, focusedSid: sid, runtimes: { [sid]: createRuntime() } }, {
    type: "event", event: { type: "history", v: PROTOCOL_VERSION, ts: 1, sid, session_id: sid,
      revision: "native", events: [], turns: summaries, detail: "summary", has_more: false },
  });
  assert.deepEqual(cold.runtimes[sid].turns.map((turn: Turn) => turn.continuation),
    [undefined, undefined, undefined, "subagent", undefined], "refresh retains the same task boundaries");
  const continued = cold.runtimes[sid].turns[3];
  assert.equal(mergeAuthoritativeTurnDetail(continued, { ...continued, continuation: undefined }).continuation,
    "subagent", "reading process detail retains summary continuation metadata");
  emit({ type: "turn_binding", msg_id: "dsh-turn-cancelled", turn_id: "dsh-turn-cancelled", autonomous: true });
  emit({ type: "turn_end", turn_id: "dsh-turn-cancelled", result: { subtype: "interrupted", duration_ms: 1, is_error: false } });
  assert.equal(state.runtimes[sid].turns.at(-1).interrupted, true,
    "a native cancellation is not rendered as successful completion");
  emit({ type: "user_msg", msg_id: "model-failed", client_msg_id: "model-failed", prompt: "question" });
  emit({ type: "turn_binding", msg_id: "model-failed", turn_id: "dsh-seq-100" });
  emit({ type: "process", item_id: "request", kind: "model", phase: "start", status: "running", turn_id: "dsh-seq-100", title: "模型请求" });
  emit({ type: "turn_end", turn_id: "dsh-seq-100", result: {
    subtype: "error_model", duration_ms: 12, is_error: true,
    error: "DeepSeek Messages cannot represent user/tool-result content reasoning",
  } });
  const failed = state.runtimes[sid].turns.at(-1);
  assert.ok(failed.done && failed.error);
  assert.notEqual(failed.interrupted, true, "a model serialization failure is not a Stop action");
  assert.equal(failed.terminalSource, "failed");
  assert.equal(failed.blocks.find((block: { kind: string }) => block.kind === "process")?.status, "failed");
  emit({ type: "dsh_state", sid: "dsh@other", connected: false, error: "连接中断" });
  assert.equal(state.runtimes[sid].dsh, undefined);
  assert.equal(state.runtimes["dsh@other"].dsh.error, "连接中断");
  assert.ok(state.runtimes[sid].turns.every((row: Turn) => row.done));
  emit({ type: "models", engine: "dsh", models: [{ id: "native-model", display_name: "Native" }], dsh_presets: [] });
  emit({ type: "models", engine: "dsh", models: [], dsh_presets: [], error: "未连接" });
  assert.deepEqual(state.catalog.dsh, []);

  // Replacing the native catalog also replaces its defaults, including absence.
  const nextModel = { id: "replacement", display_name: "Replacement", efforts: [] };
  const preferredModel = { id: "preferred", display_name: "Preferred", efforts: ["high"] };
  const otherDefaults = { claude: "custom-alias", codex: "gpt-model" };
  const otherEfforts = { claude: "high", codex: "medium" };
  for (const replacement of [
    { models: [nextModel] },
    { models: [nextModel], default_model: null, default_effort: null },
    { models: [], error: "未连接" },
    { models: [nextModel], default_model: nextModel.id },
    { models: [nextModel, preferredModel], default_model: preferredModel.id, default_effort: "high" },
  ]) {
    state = { ...initialState, catalogDefault: otherDefaults, catalogDefaultEffort: otherEfforts };
    emit({ type: "models", engine: "dsh", models: [
      { id: "removed", display_name: "Removed", efforts: ["max"] },
    ], default_model: "removed", default_effort: "max" });
    assert.equal(defaultModelFor("dsh", state.catalog, state.catalogDefault), "removed");
    emit({ type: "models", engine: "dsh", ...replacement });
    assert.deepEqual(state.catalogDefault, { ...otherDefaults,
      ...(replacement.default_model ? { dsh: replacement.default_model } : {}),
    });
    assert.deepEqual(state.catalogDefaultEffort, { ...otherEfforts,
      ...(replacement.default_effort ? { dsh: replacement.default_effort } : {}),
    });
    assert.equal(defaultModelFor("dsh", state.catalog, state.catalogDefault),
      replacement.default_model ?? replacement.models[0]?.id ?? "",
      "new sessions must not select the removed native model");
    assert.deepEqual(state.catalogDefaultCwd, initialState.catalogDefaultCwd);
  }
  // The legacy no-cwd behavior for the other engines remains unchanged.
  for (const engine of ["claude", "codex"]) {
    state = { ...initialState, catalogDefault: otherDefaults, catalogDefaultEffort: otherEfforts };
    emit({ type: "models", engine, models: [] });
    assert.deepEqual(state.catalogDefault, otherDefaults);
    assert.deepEqual(state.catalogDefaultEffort, otherEfforts);
  }

  state = { ...initialState, engine: "dsh", focusedSid: sid, sessions: [
    { session_id: sid, engine: "dsh", space: "code", state: "running", last_modified: "1" },
  ], runtimes: { [sid]: createRuntime() } };
  emit({ type: "snapshot", generation: "recovery", state: "idle", cc_session_id: sid });
  emit({ type: "user_msg", msg_id: "recover", client_msg_id: "recover", prompt: "long task" });
  emit({ type: "turn_binding", msg_id: "recover", turn_id: "dsh-seq-2" });
  emit({ type: "assistant_msg_start", turn_id: "dsh-seq-2", message_id: "work", channel: "commentary" });
  emit({ type: "delta", turn_id: "dsh-seq-2", message_id: "work", channel: "commentary", text: "still checking" });
  emit({ type: "assistant_msg_end", turn_id: "dsh-seq-2", message_id: "work", channel: "commentary" });
  const runningRow = { id: "recover", clientMsgId: "recover", prompt: "long task", done: false,
    blocks: [{ kind: "text", message_id: "work", channel: "commentary", text: "still checking", done: true }],
    processDetailState: "present", detailReasons: ["process"], detailEventCount: 1, detailLoaded: false };
  const nativeHistory = { type: "history", session_id: sid, revision: "recovery", generation: "recovery",
    authoritative: true, detail: "summary", events: [], turns: [runningRow], newest_id: "recover", has_more: false };
  // The old wrapper emitted an idle History before applying its running
  // native snapshot. Reproduce that already-painted false terminal first.
  emit({ ...nativeHistory, build_seq: 1, live_seq: seq, in_progress: false });
  assert.equal(state.runtimes[sid].turns[0].terminalSource, "idle_history_recovery");
  assert.ok(state.runtimes[sid].turns[0].error);
  emit({ type: "state", state: "running" });
  emit({ type: "ask_user", ask_id: "question", question: "Continue?", allow_text: true, options: [] });
  emit({ ...nativeHistory, build_seq: 2, live_seq: seq, in_progress: true });
  emit({ type: "turn_binding", msg_id: "recover", turn_id: "dsh-seq-2" });
  assert.equal(state.runtimes[sid].turns[0].error, undefined,
    "fresh native running history repairs a browser-only idle failure");
  assert.equal(state.runtimes[sid].turns[0].done, false);
  assert.notEqual(state.runtimes[sid].turns[0].interrupted, true);
  assert.equal(state.runtimes[sid].pendingQuestion.ask_id, "question");
  emit({ type: "turn_end", turn_id: "dsh-seq-2", result: {
    subtype: "error_blocked", duration_ms: 10, is_error: true, error: "native failure",
  } });
  const terminalError = state.runtimes[sid].turns[0].error;
  assert.ok(terminalError);
  emit({ ...nativeHistory, build_seq: 3, live_seq: seq - 2, in_progress: true });
  assert.equal(state.runtimes[sid].turns[0].error, terminalError,
    "stale running history cannot undo a real native failure");
  assert.equal(state.runtimes[sid].turns[0].done, true);
  console.log("DSH identity, retry, autonomous round and scoped state regressions passed");
} finally {
  await harness.close();
}
