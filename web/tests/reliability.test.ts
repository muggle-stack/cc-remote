import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

import {
  clearLegacyAuthMarkers,
  probeSession,
  shouldReconnectAfterSessionProbe,
} from "../src/session-auth.ts";
import {
  canEnqueueQuery,
  collectWaitingQueries,
  queuedQueryWireBytes,
  reduceTargetedRuntime,
  selectDrainCandidates,
} from "../src/runtime-drain.ts";
import { mergeInitialHistory } from "../src/history-merge.ts";
import { imageDimensions } from "../src/img.ts";
import { boundCachedTurns } from "../src/cache.ts";
import { boundRuntimeTurns, pruneRuntimeMap } from "../src/runtime-bounds.ts";
import { ImeSubmitGuard, shouldSubmitTextKey } from "../src/ime-submit.ts";
import {
  classifyBtwOpened,
  consumeDiscardedBtwSnapshot,
  makeOpenBtwCommand,
  matchesBtwRequest,
  normalizeDiffTheme,
  normalizeEngine,
} from "../src/protocol.ts";
import { RelayWs } from "../src/ws.ts";
import {
  clientSlashesFor,
  commandsFor,
  matchCommands,
  modelsFor,
} from "../src/data.ts";
import {
  createMobileViewportSync,
  type MobileViewportBindings,
  type MobileViewportEvent,
  type ViewportReading,
} from "../src/use-mobile-viewport.ts";
import type { ServerEvent } from "../src/protocol.ts";
import { clampPanelWidth, resolveSidebarSwipe } from "../src/responsive-layout.ts";

assert.equal(resolveSidebarSwipe(12, 200, 84, 205, 390, false), "open");
assert.equal(resolveSidebarSwipe(300, 200, 230, 205, 390, false), "close");
assert.equal(resolveSidebarSwipe(12, 100, 78, 240, 390, false), null,
  "a mostly vertical gesture must not navigate");
assert.equal(resolveSidebarSwipe(12, 200, 84, 205, 390, true), null,
  "an interactive vertical scroller must own its gesture");
assert.equal(clampPanelWidth(200, 1440), 360);
assert.equal(clampPanelWidth(2_000, 1440), 1_020);
assert.equal(clampPanelWidth(600, 1_000), 580);

const viewportListeners = new Map<MobileViewportEvent, Set<() => void>>();
const viewportCss = new Map<string, string>();
const viewportFrames = new Map<number, () => void>();
const viewportDelays = new Map<number, () => void>();
let nextViewportTaskId = 1;
let viewportReading: ViewportReading = {
  height: 844, layoutHeight: 844, offsetTop: 0, scale: 1,
};
let editableFocused = false;
let layoutScrollResets = 0;
const viewportBindings: MobileViewportBindings = {
  readViewport: () => viewportReading,
  setCssProperty: (name, value) => { viewportCss.set(name, value); },
  clearCssProperty: (name) => { viewportCss.delete(name); },
  listen: (event, listener) => {
    const listeners = viewportListeners.get(event) ?? new Set();
    listeners.add(listener);
    viewportListeners.set(event, listeners);
    return () => listeners.delete(listener);
  },
  requestFrame: (listener) => {
    const id = nextViewportTaskId++;
    viewportFrames.set(id, listener);
    return id;
  },
  cancelFrame: (id) => { viewportFrames.delete(id); },
  setDelay: (listener) => {
    const id = nextViewportTaskId++;
    viewportDelays.set(id, listener);
    return id;
  },
  clearDelay: (id) => { viewportDelays.delete(id); },
  isEditableFocused: () => editableFocused,
  resetLayoutScroll: () => { layoutScrollResets += 1; },
};
const flushViewportFrames = () => {
  const frames = [...viewportFrames.values()];
  viewportFrames.clear();
  for (const frame of frames) frame();
};
const emitViewport = (event: MobileViewportEvent) => {
  for (const listener of viewportListeners.get(event) ?? []) listener();
};

const stopViewportSync = createMobileViewportSync(viewportBindings);
assert.equal(viewportCss.get("--app-height"), "844px");
assert.equal(viewportCss.get("--app-offset-top"), "0px");
assert.equal(viewportCss.get("--keyboard-inset"), "0px");

viewportReading = { height: 510.25, layoutHeight: 844, offsetTop: 0, scale: 1 };
emitViewport("viewport-resize");
assert.equal(viewportCss.get("--app-height"), "844px");
flushViewportFrames();
assert.equal(viewportCss.get("--app-height"), "510.25px");
assert.equal(viewportCss.get("--keyboard-inset"), "333.75px");

viewportReading = { height: 500, layoutHeight: 844, offsetTop: 24, scale: 1 };
emitViewport("viewport-scroll");
flushViewportFrames();
assert.equal(viewportCss.get("--app-height"), "500px");
assert.equal(viewportCss.get("--app-offset-top"), "24px");
assert.equal(viewportCss.get("--keyboard-inset"), "320px");

// Pinch zoom remains user-controlled and must not be treated as a keyboard.
viewportReading = { height: 420, layoutHeight: 844, offsetTop: 30, scale: 1.5 };
emitViewport("viewport-resize");
flushViewportFrames();
assert.equal(viewportCss.get("--app-offset-top"), "0px");
assert.equal(viewportCss.get("--keyboard-inset"), "0px");

// Safari can report its pre-blur viewport for a few animation frames. Delayed
// settling rereads it and clears only the layout-level focus pan.
viewportReading = { height: 844, layoutHeight: 844, offsetTop: 0, scale: 1 };
emitViewport("focus-out");
assert.equal(viewportDelays.size, 2);
flushViewportFrames();
for (const delayed of [...viewportDelays.values()]) delayed();
viewportDelays.clear();
flushViewportFrames();
assert.equal(layoutScrollResets, 2);
assert.equal(viewportCss.get("--app-height"), "844px");

editableFocused = true;
emitViewport("orientation-change");
for (const delayed of [...viewportDelays.values()]) delayed();
viewportDelays.clear();
flushViewportFrames();
assert.equal(layoutScrollResets, 2);
editableFocused = false;

// Repeated keyboard cycles must always settle back to the full viewport instead
// of accumulating height, offset, or bottom-inset drift.
for (let cycle = 0; cycle < 10; cycle += 1) {
  viewportReading = { height: 508, layoutHeight: 844, offsetTop: 18, scale: 1 };
  emitViewport("viewport-resize");
  flushViewportFrames();
  viewportReading = { height: 844, layoutHeight: 844, offsetTop: 0, scale: 1 };
  emitViewport("focus-out");
  flushViewportFrames();
  for (const delayed of [...viewportDelays.values()]) delayed();
  viewportDelays.clear();
  flushViewportFrames();
  assert.equal(viewportCss.get("--app-height"), "844px");
  assert.equal(viewportCss.get("--app-offset-top"), "0px");
  assert.equal(viewportCss.get("--keyboard-inset"), "0px");
}
assert.equal(layoutScrollResets, 22);

stopViewportSync();
assert.equal(viewportCss.has("--app-height"), false);
assert.equal(viewportCss.has("--app-offset-top"), false);
assert.equal(viewportCss.has("--keyboard-inset"), false);
assert.equal(viewportFrames.size, 0);
assert.equal(viewportDelays.size, 0);
for (const listeners of viewportListeners.values()) assert.equal(listeners.size, 0);

assert.equal(shouldSubmitTextKey({
  key: "Enter", shiftKey: false, isComposing: false, keyCode: 13,
}), true);
assert.equal(shouldSubmitTextKey({
  key: "Enter", shiftKey: true, isComposing: false, keyCode: 13,
}), false);
assert.equal(shouldSubmitTextKey({
  key: "Enter", shiftKey: false, isComposing: true, keyCode: 13,
}), false);
assert.equal(shouldSubmitTextKey({
  key: "Enter", shiftKey: false, isComposing: false, keyCode: 229,
}), false);
assert.equal(shouldSubmitTextKey({
  key: "Space", shiftKey: false, isComposing: false, keyCode: 32,
}), false);

const imeSubmit = new ImeSubmitGuard();
assert.equal(imeSubmit.shouldSubmitKey({
  key: "Enter", shiftKey: false, isComposing: false, keyCode: 13,
}), true);
imeSubmit.startComposition();
assert.equal(imeSubmit.shouldSubmitKey({
  key: "Enter", shiftKey: false, isComposing: false, keyCode: 13,
}), false);
assert.equal(imeSubmit.shouldCommitBeforeButtonSubmit(), true);
imeSubmit.endComposition();
assert.equal(imeSubmit.shouldSubmitKey({
  key: "Enter", shiftKey: false, isComposing: false, keyCode: 13,
}), true);
assert.equal(imeSubmit.shouldCommitBeforeButtonSubmit(), false);

// Every text-entry surface that can submit outside the three composers must use
// the same IME guard. Browser-level composition ordering remains an E2E release
// gate; this integration check prevents a raw Enter handler from bypassing the
// shared path before that suite runs.
for (const filename of [
  "QuestionSheet.tsx",
  "SessionsSidebar.tsx",
  "DirPicker.tsx",
  "LoginForm.tsx",
  "ForkWorktreeSheet.tsx",
]) {
  const source = readFileSync(resolve(process.cwd(), "src/components", filename), "utf8");
  assert.match(source, /useImeSubmit/);
  assert.match(source, /onCompositionStart/);
  assert.match(source, /nativeEvent\.isComposing/);
  assert.match(source, /nativeEvent\.keyCode/);
}

let requested = "";
const authenticated = await probeSession(async (input, init) => {
  requested = input;
  assert.equal(init.credentials, "same-origin");
  assert.equal(init.cache, "no-store");
  assert.equal(init.signal.aborted, false);
  return { ok: true, status: 200 };
}, 100);
assert.equal(requested, "/api/session");
assert.equal(authenticated, "authenticated");

const unauthorized = await probeSession(
  async () => ({ ok: false, status: 401 }), 100);
assert.equal(unauthorized, "unauthorized");
assert.equal(shouldReconnectAfterSessionProbe(unauthorized), false);

const serverError = await probeSession(
  async () => ({ ok: false, status: 503 }), 100);
assert.equal(serverError, "unavailable");
assert.equal(shouldReconnectAfterSessionProbe(serverError), true);

const networkError = await probeSession(async () => {
  throw new Error("offline");
}, 100);
assert.equal(networkError, "unavailable");
assert.equal(shouldReconnectAfterSessionProbe(networkError), true);

const timeout = await probeSession(
  async () => new Promise(() => {}), 1);
assert.equal(timeout, "unavailable");

const removed: string[] = [];
clearLegacyAuthMarkers({ removeItem: (key) => { removed.push(key); } });
assert.deepEqual(removed, ["cc_remote_session", "cc_remote_authenticated"]);

const pending = {
  prompt: "pending-a",
  images: [{ media_type: "image/png", data: "img" }],
};
const queued = {
  prompt: "queued-b",
  files: [{ name: "note.txt", media_type: "text/plain", data: "file" }],
};
const runtimes = {
  a: { state: "idle", syncReady: true, pendingSend: pending, queue: [{ prompt: "later-a" }] },
  b: { state: "idle", syncReady: true, pendingSend: null, queue: [queued] },
  c: { state: "running", syncReady: true, pendingSend: null, queue: [{ prompt: "busy-c" }] },
  d: { state: "idle", syncReady: true, pendingSend: null, queue: [{ prompt: "draining-d" }] },
  e: { state: "idle", syncReady: false, pendingSend: null, queue: [{ prompt: "stale-e" }] },
  f: { state: "idle", syncReady: true, external: true, pendingSend: null, queue: [{ prompt: "external-f" }] },
};
assert.deepEqual(
  selectDrainCandidates(runtimes, new Set(["d"]), true, true),
  [
    { sid: "a", source: "pending", query: pending },
    { sid: "b", source: "queue", query: queued },
  ],
);
assert.deepEqual(selectDrainCandidates(runtimes, new Set(), false, true), []);
assert.deepEqual(selectDrainCandidates(runtimes, new Set(), true, false), []);

const sizedQuery = {
  prompt: "queued",
  files: [{ filename: "secret.txt", data: "sensitive-body" }],
};
const sizedQueryBytes = queuedQueryWireBytes(sizedQuery);
assert.equal(canEnqueueQuery([], sizedQuery, 32, sizedQueryBytes), true);
assert.equal(canEnqueueQuery([], sizedQuery, 32, sizedQueryBytes - 1), false);
assert.equal(canEnqueueQuery([sizedQuery], sizedQuery, 1, sizedQueryBytes * 3), false);
assert.equal(canEnqueueQuery(
  [sizedQuery], sizedQuery, 32, sizedQueryBytes * 2), true);
assert.deepEqual(collectWaitingQueries({
  one: { queue: ["queued"], pendingSend: "replace-me" },
  two: { queue: [], pendingSend: "other-pending" },
}, "one"), ["queued", "other-pending"]);

const a = {
  state: "idle", syncReady: true, pendingSend: null, queue: [{ prompt: "a" }],
  turns: [{ id: "a-old", prompt: "old" }],
};
const b = {
  state: "idle", syncReady: true, pendingSend: { prompt: "pending-b" },
  queue: [{ prompt: "b0" }, { prompt: "b1" }],
  turns: [{ id: "b-old", prompt: "old" }],
};
const runtimeMap = { a, b };
const withTurn = reduceTargetedRuntime(runtimeMap, "b", {
  type: "query_sent", turn: { id: "b-new", prompt: "new" },
});
assert.strictEqual(withTurn.a, a);
assert.deepEqual(withTurn.a.turns.map((turn) => turn.id), ["a-old"]);
assert.deepEqual(withTurn.b.turns.map((turn) => turn.id), ["b-old", "b-new"]);

const dequeued = reduceTargetedRuntime(withTurn, "b", { type: "dequeue_at", i: 0 });
assert.deepEqual(dequeued.a.queue.map((query) => query.prompt), ["a"]);
assert.deepEqual(dequeued.b.queue.map((query) => query.prompt), ["b1"]);

const cleared = reduceTargetedRuntime(dequeued, "b", { type: "clear_pending" });
assert.equal(cleared.a.pendingSend, null);
assert.equal(cleared.b.pendingSend, null);
assert.strictEqual(
  reduceTargetedRuntime(cleared, "missing", { type: "clear_pending" }),
  cleared,
);

const transcriptTurn = {
  id: "engine-id", prompt: "same prompt", done: true, ts: 1000,
  blocks: [{ kind: "text" as const, message_id: "engine-text", text: "answer", done: true }],
};
const optimisticTurn = {
  id: "client-id", prompt: "same prompt", done: false, ts: 1100,
  blocks: [{ kind: "text" as const, message_id: "live-text", text: "answer tail", done: false }],
};
const laggingDone = {
  id: "client-lag", prompt: "not flushed", done: true, ts: 2000, blocks: [],
};
const mergedHistory = mergeInitialHistory(
  [transcriptTurn], [optimisticTurn, laggingDone]);
assert.deepEqual(mergedHistory.map((turn) => turn.id), ["client-id", "client-lag"]);
assert.equal(mergedHistory[0].done, true);
assert.equal(mergedHistory[0].blocks.length, 1);
assert.equal(mergedHistory[1].prompt, "not flushed");

const repeatedOld = {
  id: "old-engine", prompt: "继续", done: true, ts: 10_000,
  blocks: [{ kind: "text" as const, message_id: "old-answer", text: "old", done: true }],
};
const repeatedCurrent = {
  id: "current-client", prompt: "继续", done: false, ts: 15_000, blocks: [],
};
const repeatedMerged = mergeInitialHistory([repeatedOld], [repeatedCurrent]);
assert.deepEqual(repeatedMerged.map((turn) => turn.id), ["old-engine", "current-client"]);
assert.equal(repeatedMerged[1].done, false);

const delayedEcho = mergeInitialHistory(
  [{ ...transcriptTurn, id: "engine-delayed", ts: 20_000 }],
  [{ ...optimisticTurn, id: "client-delayed", ts: 22_500 }],
);
assert.deepEqual(delayedEcho.map((turn) => turn.id), ["client-delayed"]);

// History and live ids are generated by different streams. Multiple blocks in
// the same channel must pair in order/content instead of all merging into the
// last commentary block.
const multiCommentHistory = {
  id: "multi-history", prompt: "inspect", done: true, ts: 30_000,
  blocks: [
    { kind: "text" as const, message_id: "hist-a", text: "A", done: true,
      channel: "commentary" as const },
    { kind: "tool" as const, message_id: "hist-a", tool_use_id: "shared-tool",
      tool: "shell", input: {}, done: true,
      result: { content: "ok", is_error: false } },
    { kind: "text" as const, message_id: "hist-b", text: "B", done: true,
      channel: "commentary" as const },
  ],
};
const multiCommentLive = {
  id: "multi-live", prompt: "inspect", done: true, ts: 30_100,
  blocks: [
    { kind: "text" as const, message_id: "live-a", text: "A", done: true,
      channel: "commentary" as const },
    { kind: "tool" as const, message_id: "live-a", tool_use_id: "shared-tool",
      tool: "shell", input: {}, done: true,
      result: { content: "ok", is_error: false } },
    { kind: "text" as const, message_id: "live-b", text: "B", done: true,
      channel: "commentary" as const },
  ],
};
const multiCommentMerged = mergeInitialHistory(
  [multiCommentHistory], [multiCommentLive]);
assert.deepEqual(multiCommentMerged[0].blocks.filter(
  (block) => block.kind === "text").map((block) => block.text), ["A", "B"]);

// A complete transcript must not be reopened forever by stale cache state.
const terminalHistory = {
  id: "terminal-history", prompt: "run", done: true, ts: 40_000,
  blocks: [
    { kind: "tool" as const, message_id: "tool-msg", tool_use_id: "tool-terminal",
      tool: "shell", input: {}, done: true,
      result: { content: "ok", is_error: false } },
    { kind: "process" as const, item_id: "agent-terminal", processKind: "agent" as const,
      phase: "end" as const, status: "succeeded" as const, title: "代理完成", done: true },
  ],
};
const staleLive = {
  id: "terminal-live", prompt: "run", done: false, ts: 40_100,
  blocks: [
    { kind: "tool" as const, message_id: "tool-msg-live", tool_use_id: "tool-terminal",
      tool: "shell", input: {}, done: false },
    { kind: "process" as const, item_id: "agent-terminal", processKind: "agent" as const,
      phase: "update" as const, status: "running" as const, title: "代理运行中", done: false },
  ],
};
const terminalMerged = mergeInitialHistory([terminalHistory], [staleLive]);
assert.equal(terminalMerged[0].blocks.every((block) => block.done), true);
assert.equal(terminalMerged[0].blocks[0].kind === "tool"
  && terminalMerged[0].blocks[0].result?.content, "ok");
assert.equal(terminalMerged[0].blocks[1].kind === "process"
  && terminalMerged[0].blocks[1].title, "代理完成");
const openTailMerged = mergeInitialHistory(
  [terminalHistory], [staleLive], { preserveLiveTailOpen: true });
assert.equal(openTailMerged[0].blocks.some((block) => !block.done), true);

// Never collapse two real repeated turns just because both prompt and response
// happen to be identical. Stable ids remain the only authoritative identity.
const repeatedAnswerTurns = [
  { id: "repeat-1", prompt: "在？", done: true, ts: 5000, doneTs: 6000,
    blocks: [{ kind: "text" as const, message_id: "repeat-answer-1",
      text: "在的", done: true }] },
  { id: "repeat-2", prompt: "在？", done: true, ts: 7000, doneTs: 8000,
    blocks: [{ kind: "text" as const, message_id: "repeat-answer-2",
      text: "在的", done: true }] },
];
assert.deepEqual(
  mergeInitialHistory(repeatedAnswerTurns, []).map((turn) => turn.id),
  ["repeat-1", "repeat-2"],
);

// Exact production race at the pure merge boundary: a focus-triggered History
// synthesizes TurnEnd while the matching live tail is still running. Preserve
// that open tail, let the live answer finish in place, then reconcile the full
// transcript without creating a second assistant-only turn.
const partialHistory = {
  id: "engine-active", prompt: "在？", done: true, ts: 10_000, doneTs: 11_000,
  forkPointId: "codex-turn-a",
  blocks: [] as Array<{ kind: "text"; message_id: string; text: string; done: boolean }>,
};
const liveActive = {
  id: "client-active", prompt: "在？", done: false, ts: 10_100,
  blocks: [{ kind: "text" as const, message_id: "live-active-answer",
    text: "", done: false }],
};
const activeMerge = mergeInitialHistory(
  [partialHistory], [liveActive], { preserveLiveTailOpen: true });
assert.equal(activeMerge.length, 1);
assert.equal(activeMerge[0].done, false);
assert.equal(activeMerge[0].doneTs, undefined);
assert.equal(activeMerge[0].forkPointId, "codex-turn-a");
const liveFinished = [{
  ...activeMerge[0], done: true, doneTs: 12_000,
  blocks: activeMerge[0].blocks.map((block) => block.kind === "text"
    ? { ...block, text: "only once", done: true } : block),
}];
const completeHistory = [{
  ...partialHistory, doneTs: 12_000,
  blocks: [{ kind: "text" as const, message_id: "engine-active-answer",
    text: "only once", done: true }],
}];
const activeReconciled = mergeInitialHistory(completeHistory, liveFinished);
assert.equal(activeReconciled.length, 1);
assert.deepEqual(activeReconciled[0].blocks.map((block) => block.kind === "text"
  ? block.text : "tool"), ["only once"]);

// A Codex goal continuation has no user prompt. Live and rollout use different
// display ids, but their authoritative app-server turn id must dedupe them.
const assistantOnlyMerged = mergeInitialHistory(
  [{ id: "history-agent-message", forkPointId: "goal-turn-1", prompt: "",
    done: true, blocks: [{ kind: "text" as const, message_id: "history-answer",
      text: "goal complete", done: true, channel: "final" as const }] }],
  [{ id: "goal-turn-1", forkPointId: "goal-turn-1", prompt: "",
    done: true, blocks: [{ kind: "text" as const, message_id: "live-answer",
      text: "goal complete", done: true, channel: "final" as const }] }],
);
assert.equal(assistantOnlyMerged.length, 1);
assert.equal(assistantOnlyMerged[0].id, "goal-turn-1");
assert.equal(assistantOnlyMerged[0].blocks.filter((block) => block.kind === "text").length, 1);

// Exercise the real reducer through Vite's zero-network SSR loader. The plain
// Node test output cannot import reducer.js directly because the browser build
// intentionally uses extensionless module specifiers.
const reducerHarness = await createServer({
  root: process.cwd(),
  appType: "custom",
  logLevel: "silent",
  server: { middlewareMode: true, watch: null },
});
try {
  const {
    createRuntime, initialState, reduce, MAX_TURN_BLOCKS, MAX_TURN_BLOCK_CHARS,
    OMITTED_PROCESS_ITEM_ID,
  } = await reducerHarness.ssrLoadModule("/src/reducer.ts");
  const event = (body: Record<string, unknown>): ServerEvent => ({
    v: 9, ts: 10, ...body,
  } as ServerEvent);
  const unannounced = createRuntime();
  assert.equal(unannounced.model, "");
  assert.equal(unannounced.effort, "");
  assert.equal(unannounced.perm, "");
  assert.equal(unannounced.fast, null);
  const sid = "race-a";
  const otherSid = "race-b";
  const untouched = {
    id: "b-turn", prompt: "other", done: true, blocks: [], ts: 1000,
  };
  let state = {
    ...initialState,
    connState: "connected",
    wrapperOnline: true,
    focusedSid: sid,
    newChat: { cwd: "~", model: null, effort: null },
    runtimes: {
      [sid]: { ...createRuntime(), state: "running", syncReady: true },
      [otherSid]: { ...createRuntime(), turns: [untouched], syncReady: true },
    },
  };
  // Claude has a static presentation catalog, but its explicit defaults come
  // from cwd-aware settings. An empty models array must still update them.
  state = reduce(state, { type: "event", event: event({
    type: "models", engine: "claude", models: [],
    default_model: "claude-mythos-5[1m]", default_effort: "max",
    cwd: "~",
  }) });
  assert.equal(state.catalogDefault.claude, "claude-mythos-5");
  assert.equal(state.catalogDefaultEffort.claude, "max");
  const { NewChatView } = await reducerHarness.ssrLoadModule(
    "/src/components/NewChatView.tsx");
  const newChatMarkup = renderToStaticMarkup(createElement(NewChatView, {
    cwd: "~", engine: "claude",
    onPickCwd: () => {},
    onSend: () => true,
  }));
  const codexNewChatMarkup = renderToStaticMarkup(createElement(NewChatView, {
    cwd: "~", engine: "codex",
    onPickCwd: () => {},
    onSend: () => true,
  }));
  for (const markup of [newChatMarkup, codexNewChatMarkup]) {
    assert.match(markup, /添加图片或文件/);
    assert.match(markup, />开始</);
    assert.doesNotMatch(markup, /本机默认|默认 ·|选择模型|思考强度/);
  }
  assert.doesNotMatch(codexNewChatMarkup, /不询问|Plan|标准/);
  state = { ...state,
    newChat: { cwd: "/other", model: null, effort: null } };
  state = reduce(state, { type: "event", event: event({
    type: "models", engine: "claude", models: [],
    default_model: "claude-sonnet-5", default_effort: "high", cwd: "/other",
  }) });
  state = reduce(state, { type: "event", event: event({
    type: "models", engine: "claude", models: [],
    default_model: "claude-mythos-5", default_effort: "max", cwd: "~",
  }) });
  assert.equal(state.catalogDefault.claude, "claude-sonnet-5");
  assert.equal(state.catalogDefaultEffort.claude, "high");
  assert.equal(state.catalogDefaultCwd.claude, "/other");
  state = reduce(state, { type: "event", event: event({
    type: "models", engine: "claude", models: [],
    default_model: null, default_effort: null, cwd: "/other",
  }) });
  assert.equal(state.catalogDefault.claude, undefined);
  assert.equal(state.catalogDefaultEffort.claude, undefined);
  assert.equal(state.catalogDefaultCwd.claude, "/other");
  // The newest history page restores both authoritative settings. An older
  // pagination page cannot roll either one back, even if a stale server includes
  // control rows that should only appear on the newest page.
  state = reduce(state, { type: "event", event: event({
    type: "history", sid, session_id: sid, has_more: true,
    events: [
      event({ type: "model", sid, model: "claude-mythos-5" }),
      event({ type: "effort", sid, effort: "max" }),
    ],
  }) });
  assert.equal(state.runtimes[sid].model, "claude-mythos-5");
  assert.equal(state.runtimes[sid].effort, "max");
  state = reduce(state, { type: "event", event: event({
    type: "history", sid, session_id: sid, before: "older-turn",
    has_more: false,
    events: [
      event({ type: "model", sid, model: "claude-sonnet-5" }),
      event({ type: "effort", sid, effort: "low" }),
    ],
  }) });
  assert.equal(state.runtimes[sid].model, "claude-mythos-5");
  assert.equal(state.runtimes[sid].effort, "max");
  const approvalBeforePlan = state.runtimes[sid].perm;
  state = reduce(state, { type: "event", event: event({
    type: "collaboration_mode", sid, mode: "plan",
  }) });
  assert.equal(state.runtimes[sid].collaborationMode, "plan");
  assert.equal(state.runtimes[sid].perm, approvalBeforePlan);
  assert.equal(state.runtimes[otherSid].collaborationMode, "default");
  state = reduce(state, { type: "set_collaboration_mode", mode: "default" });
  assert.equal(state.runtimes[sid].collaborationMode, "default");
  assert.equal(state.runtimes[sid].perm, approvalBeforePlan);
  const cachedSid = "cached-v5-codex";
  state = reduce(state, { type: "hydrate_cache", sid: cachedSid, turns: [{
    id: "cached-turn", codexTurnId: "legacy-turn-id", prompt: "旧缓存",
    done: true, blocks: [],
  }] });
  assert.equal(state.runtimes[cachedSid].turns[0].forkPointId, "legacy-turn-id");
  state = reduce(state, {
    type: "query_sent", sid, prompt: "在？", msg_id: "client-a", ts: 10_000,
  });
  for (const live of [
    event({ type: "user_msg", sid, msg_id: "client-a", prompt: "在？", ts: 10.1 }),
    event({ type: "assistant_msg_start", sid, message_id: "live-answer" }),
  ]) state = reduce(state, { type: "event", event: live });

  state = reduce(state, { type: "event", event: event({
    type: "history", sid, session_id: sid, in_progress: true, has_more: false,
    events: [
      event({ type: "user_msg", sid, msg_id: "engine-a", prompt: "在？", ts: 10 }),
      event({ type: "turn_end", sid, ts: 11, turn_id: "codex-turn-a",
        result: { subtype: "success", duration_ms: 0, is_error: false } }),
    ],
  }) });
  assert.equal(state.runtimes[sid].turns.length, 1);
  assert.equal(state.runtimes[sid].turns[0].done, false);
  assert.equal(state.runtimes[sid].turns[0].forkPointId, "codex-turn-a");

  for (const live of [
    event({ type: "delta", sid, message_id: "live-answer", text: "only once" }),
    event({ type: "assistant_msg_end", sid, message_id: "live-answer" }),
    event({ type: "turn_end", sid, ts: 12, turn_id: "codex-turn-a",
      result: { subtype: "success", duration_ms: 2000, is_error: false } }),
  ]) state = reduce(state, { type: "event", event: live });

  state = reduce(state, { type: "event", event: event({
    type: "history", sid, session_id: sid, in_progress: false, has_more: false,
    events: [
      event({ type: "user_msg", sid, msg_id: "engine-a", prompt: "在？", ts: 10 }),
      event({ type: "assistant_msg_start", sid, message_id: "engine-answer" }),
      event({ type: "delta", sid, message_id: "engine-answer", text: "only once" }),
      event({ type: "assistant_msg_end", sid, message_id: "engine-answer" }),
      event({ type: "turn_end", sid, ts: 12, turn_id: "codex-turn-a",
        result: { subtype: "success", duration_ms: 2000, is_error: false } }),
    ],
  }) });
  assert.equal(state.runtimes[sid].turns.length, 1);
  assert.equal(state.runtimes[sid].turns[0].forkPointId, "codex-turn-a");
  assert.deepEqual(state.runtimes[sid].turns[0].blocks.map(
    (block: { kind: string; text?: string }) => block.kind === "text" ? block.text : "tool"),
  ["only once"]);
  assert.deepEqual(state.runtimes[otherSid].turns, [untouched]);

  const richSid = "rich-process";
  state = {
    ...state,
    focusedSid: richSid,
    runtimes: {
      ...state.runtimes,
      [richSid]: { ...createRuntime(), state: "running", syncReady: true },
    },
  };
  state = reduce(state, { type: "query_sent", sid: richSid, prompt: "实现功能",
    msg_id: "rich-turn", ts: 30_000 });
  const richEvents = [
    event({ type: "assistant_msg_start", sid: richSid, message_id: "comment-1", channel: "commentary" }),
    event({ type: "delta", sid: richSid, message_id: "comment-1", channel: "commentary", text: "先检查代码。" }),
    event({ type: "assistant_msg_end", sid: richSid, message_id: "comment-1", channel: "commentary" }),
    event({ type: "tool_use", sid: richSid, message_id: "comment-1", tool_use_id: "cmd-1",
      tool: "shell", category: "command", input: { command: "npm test" } }),
    event({ type: "tool_delta", sid: richSid, tool_use_id: "cmd-1", stream: "output", delta: "ok\n" }),
    event({ type: "tool_result", sid: richSid, tool_use_id: "cmd-1", content: "ok\n",
      is_error: false, status: "succeeded", exit_code: 0, duration_ms: 1250 }),
    event({ type: "turn_plan", sid: richSid, item_id: "plan-1", turn_id: "turn-rich",
      explanation: "执行计划", plan: [{ step: "检查", status: "completed" }] }),
    event({ type: "process", sid: richSid, item_id: "hook-1", kind: "hook", phase: "end",
      status: "succeeded", turn_id: "turn-rich", title: "Hook 完成", duration_ms: 20 }),
    event({ type: "turn_diff", sid: richSid, item_id: "diff-1", turn_id: "turn-rich",
      diff: "diff --git a/a b/a" }),
    event({ type: "assistant_msg_start", sid: richSid, message_id: "final-1", channel: "final" }),
    event({ type: "delta", sid: richSid, message_id: "final-1", channel: "final", text: "已经完成。" }),
    event({ type: "assistant_msg_end", sid: richSid, message_id: "final-1", channel: "final" }),
    event({ type: "turn_end", sid: richSid, ts: 33, turn_id: "turn-rich",
      result: { subtype: "success", duration_ms: 3000, is_error: false } }),
  ];
  for (const richEvent of richEvents) {
    state = reduce(state, { type: "event", event: richEvent });
  }
  const richTurn = state.runtimes[richSid].turns[0];
  assert.equal(richTurn.durationMs, 3000);
  assert.deepEqual(richTurn.blocks.filter((block: { kind: string }) => block.kind === "text")
    .map((block: { channel?: string }) => block.channel), ["commentary", "final"]);
  const commandBlock = richTurn.blocks.find((block: { kind: string }) => block.kind === "tool") as {
    output?: string; result?: { exit_code?: number; duration_ms?: number };
  };
  assert.equal(commandBlock.output, "ok\n");
  assert.equal(commandBlock.result?.exit_code, 0);
  assert.equal(commandBlock.result?.duration_ms, 1250);
  assert.equal(richTurn.blocks.filter((block: { kind: string }) => block.kind === "process").length, 3);

  // Claude can reveal only at Result that a stop_reason=null text block was the
  // final answer. A repeated End for the same id must reclassify in place, while
  // an End for an unknown id must never manufacture an empty text block/turn.
  const correctedSid = "claude-final-correction";
  state = {
    ...state,
    runtimes: {
      ...state.runtimes,
      [correctedSid]: { ...createRuntime(), state: "running", syncReady: true },
    },
  };
  state = reduce(state, { type: "query_sent", sid: correctedSid, prompt: "回答",
    msg_id: "corrected-turn", ts: 30_500 });
  for (const correctionEvent of [
    event({ type: "assistant_msg_start", sid: correctedSid,
      message_id: "ambiguous-answer", channel: "unknown" }),
    event({ type: "delta", sid: correctedSid,
      message_id: "ambiguous-answer", channel: "unknown", text: "真实正文" }),
    event({ type: "assistant_msg_end", sid: correctedSid,
      message_id: "ambiguous-answer", channel: "commentary" }),
    event({ type: "assistant_msg_end", sid: correctedSid,
      message_id: "ambiguous-answer", channel: "final" }),
    event({ type: "assistant_msg_end", sid: correctedSid,
      message_id: "missing-answer", channel: "final" }),
  ]) state = reduce(state, { type: "event", event: correctionEvent });
  const correctedTurns = state.runtimes[correctedSid].turns;
  assert.equal(correctedTurns.length, 1);
  assert.equal(correctedTurns[0].blocks.length, 1);
  assert.deepEqual(correctedTurns[0].blocks[0], {
    kind: "text", message_id: "ambiguous-answer", text: "真实正文",
    done: true, channel: "final",
  });

  // A compromised/noisy upstream must not grow an active turn without bound
  // before the normal completed-turn runtime eviction can run.
  const noisySid = "bounded-rich-delta";
  state = {
    ...state,
    runtimes: {
      ...state.runtimes,
      [noisySid]: { ...createRuntime(), state: "running", syncReady: true },
    },
  };
  state = reduce(state, { type: "query_sent", sid: noisySid, prompt: "stream",
    msg_id: "noisy-turn", ts: 31_000 });
  state = reduce(state, { type: "event", event: event({
    type: "tool_use", sid: noisySid, message_id: "noisy-msg",
    tool_use_id: "noisy-tool", tool: "shell", category: "command", input: {},
  }) });
  const noisyChunk = "x".repeat(512 * 1024);
  for (let index = 0; index < 6; index += 1) {
    state = reduce(state, { type: "event", event: event({
      type: "tool_delta", sid: noisySid, tool_use_id: "noisy-tool",
      stream: "output", delta: noisyChunk,
    }) });
  }
  const noisyTool = state.runtimes[noisySid].turns[0].blocks.find(
    (block: { kind: string }) => block.kind === "tool") as { output?: string };
  assert.equal(noisyTool.output?.length, 2 * 1024 * 1024);

  const { finalTextBlocks, hasActiveProcess } = await reducerHarness.ssrLoadModule(
    "/src/process-blocks.ts");
  state = reduce(state, { type: "event", event: event({
    type: "error", sid: noisySid, msg_id: "noisy-turn",
    code: "engine_failed", message: "stream stopped",
  }) });
  const failedNoisyTurn = state.runtimes[noisySid].turns[0];
  assert.equal(failedNoisyTurn.done, true);
  assert.equal(hasActiveProcess(failedNoisyTurn.blocks), false);
  const failedNoisyTool = failedNoisyTurn.blocks.find(
    (block: { kind: string }) => block.kind === "tool") as {
      done: boolean; result?: { is_error: boolean; status?: string | null };
    };
  assert.equal(failedNoisyTool.done, true);
  assert.equal(failedNoisyTool.result?.is_error, true);
  assert.equal(failedNoisyTool.result?.status, "failed");

  // A goal/automatic continuation can produce thousands of distinct items in
  // one still-running turn. Bound the item count (not only each item's bytes),
  // retain known final answers and newest live activity, and keep terminal
  // updates working for retained blocks.
  const boundedBlocksSid = "bounded-turn-blocks";
  state = {
    ...state,
    runtimes: {
      ...state.runtimes,
      [boundedBlocksSid]: { ...createRuntime(), state: "running", syncReady: true },
    },
  };
  state = reduce(state, { type: "query_sent", sid: boundedBlocksSid,
    prompt: "long goal", msg_id: "bounded-block-turn", ts: 31_500 });
  for (const finalEvent of [
    event({ type: "assistant_msg_start", sid: boundedBlocksSid,
      message_id: "early-final", channel: "final" }),
    event({ type: "delta", sid: boundedBlocksSid,
      message_id: "early-final", channel: "final", text: "必须保留的最终答案" }),
    event({ type: "assistant_msg_end", sid: boundedBlocksSid,
      message_id: "early-final", channel: "final" }),
  ]) state = reduce(state, { type: "event", event: finalEvent });

  const generatedBlocks = MAX_TURN_BLOCKS + 50;
  for (let index = 0; index < generatedBlocks; index += 1) {
    const suffix = String(index);
    const events = index % 5 === 0 ? [
      event({ type: "assistant_msg_start", sid: boundedBlocksSid,
        message_id: `comment-${suffix}`, channel: "commentary" }),
      event({ type: "delta", sid: boundedBlocksSid,
        message_id: `comment-${suffix}`, channel: "commentary", text: suffix }),
      event({ type: "assistant_msg_end", sid: boundedBlocksSid,
        message_id: `comment-${suffix}`, channel: "commentary" }),
    ] : index % 5 === 1 ? [
      event({ type: "tool_use", sid: boundedBlocksSid,
        message_id: `comment-${suffix}`, tool_use_id: `tool-${suffix}`,
        tool: "shell", category: "command", input: { command: "true" } }),
      event({ type: "tool_result", sid: boundedBlocksSid,
        tool_use_id: `tool-${suffix}`, content: "ok", is_error: false,
        status: "succeeded" }),
    ] : index % 5 === 2 ? [
      event({ type: "process", sid: boundedBlocksSid,
        item_id: `process-${suffix}`, kind: "hook", phase: "end",
        status: "succeeded", title: `Hook ${suffix}` }),
    ] : index % 5 === 3 ? [
      event({ type: "turn_plan", sid: boundedBlocksSid,
        item_id: `plan-${suffix}`, explanation: suffix,
        plan: [{ step: suffix, status: "completed" }] }),
    ] : [
      event({ type: "turn_diff", sid: boundedBlocksSid,
        item_id: `diff-${suffix}`, diff: `+${suffix}` }),
    ];
    for (const generatedEvent of events) {
      state = reduce(state, { type: "event", event: generatedEvent });
    }
  }
  state = reduce(state, { type: "event", event: event({
    type: "process", sid: boundedBlocksSid, item_id: "latest-active",
    kind: "agent", phase: "start", status: "running", title: "最新活动",
  }) });
  let boundedBlockTurn = state.runtimes[boundedBlocksSid].turns[0];
  assert.equal(boundedBlockTurn.blocks.length, MAX_TURN_BLOCKS);
  assert.equal(boundedBlockTurn.blocks.filter((block: { kind: string; item_id?: string }) =>
    block.kind === "process" && block.item_id === OMITTED_PROCESS_ITEM_ID).length, 1);
  assert.ok(boundedBlockTurn.blocks.some(
    (block: { kind: string; message_id?: string; channel?: string }) =>
      block.kind === "text" && block.message_id === "early-final"
      && block.channel === "final"));
  assert.ok(boundedBlockTurn.blocks.some(
    (block: { kind: string; item_id?: string; done: boolean }) =>
      block.kind === "process" && block.item_id === "latest-active" && !block.done));
  for (const expectedId of ["comment-305", "tool-301", "process-302", "plan-303", "diff-304"]) {
    assert.ok(boundedBlockTurn.blocks.some((block: {
      kind: string; message_id?: string; tool_use_id?: string; item_id?: string;
    }) => block.message_id === expectedId || block.tool_use_id === expectedId
      || block.item_id === expectedId));
  }

  state = reduce(state, { type: "event", event: event({
    type: "process", sid: boundedBlocksSid, item_id: "latest-active",
    kind: "agent", phase: "end", status: "succeeded", title: "最新活动完成",
  }) });
  boundedBlockTurn = state.runtimes[boundedBlocksSid].turns[0];
  const closedLatest = boundedBlockTurn.blocks.find((block: {
    kind: string; item_id?: string;
  }) => block.kind === "process" && block.item_id === "latest-active") as {
    done: boolean; status: string;
  };
  assert.equal(closedLatest.done, true);
  assert.equal(closedLatest.status, "succeeded");

  for (const finalEvent of [
    event({ type: "assistant_msg_start", sid: boundedBlocksSid,
      message_id: "late-final", channel: "final" }),
    event({ type: "delta", sid: boundedBlocksSid,
      message_id: "late-final", channel: "final", text: "最新最终答案" }),
    event({ type: "assistant_msg_end", sid: boundedBlocksSid,
      message_id: "late-final", channel: "final" }),
  ]) state = reduce(state, { type: "event", event: finalEvent });
  boundedBlockTurn = state.runtimes[boundedBlocksSid].turns[0];
  assert.equal(boundedBlockTurn.blocks.length, MAX_TURN_BLOCKS);
  assert.ok(boundedBlockTurn.blocks.some((block: {
    kind: string; message_id?: string; channel?: string;
  }) => block.kind === "text" && block.message_id === "late-final"
    && block.channel === "final"));

  const boundedPayloadSid = "bounded-turn-payload";
  state = {
    ...state,
    runtimes: {
      ...state.runtimes,
      [boundedPayloadSid]: { ...createRuntime(), state: "running", syncReady: true },
    },
  };
  state = reduce(state, { type: "query_sent", sid: boundedPayloadSid,
    prompt: "large diffs", msg_id: "bounded-payload-turn", ts: 31_700 });
  const maximumDiff = "d".repeat(2 * 1024 * 1024);
  for (let index = 0; index < 10; index += 1) {
    state = reduce(state, { type: "event", event: event({
      type: "turn_diff", sid: boundedPayloadSid, item_id: `large-diff-${index}`,
      diff: maximumDiff,
    }) });
  }
  const payloadBlocks = state.runtimes[boundedPayloadSid].turns[0].blocks;
  const retainedDiffChars = payloadBlocks.reduce((total: number, block: {
    kind: string; diff?: string | null;
  }) => total + (block.kind === "process" ? (block.diff?.length ?? 0) : 0), 0);
  assert.ok(retainedDiffChars <= MAX_TURN_BLOCK_CHARS);
  assert.ok(payloadBlocks.some((block: { kind: string; item_id?: string }) =>
    block.kind === "process" && block.item_id === "large-diff-9"));
  assert.equal(payloadBlocks.filter((block: { kind: string; item_id?: string }) =>
    block.kind === "process" && block.item_id === OMITTED_PROCESS_ITEM_ID).length, 1);

  // Authoritative History is reduced through the same bounded window and must
  // not recreate an oversized turn after a refresh.
  const boundedHistorySid = "bounded-history-blocks";
  const boundedHistoryEvents: ServerEvent[] = [
    event({ type: "user_msg", sid: boundedHistorySid,
      msg_id: "bounded-history-turn", prompt: "history" }),
    event({ type: "assistant_msg_start", sid: boundedHistorySid,
      message_id: "history-final", channel: "final" }),
    event({ type: "delta", sid: boundedHistorySid,
      message_id: "history-final", channel: "final", text: "历史最终答案" }),
    event({ type: "assistant_msg_end", sid: boundedHistorySid,
      message_id: "history-final", channel: "final" }),
  ];
  for (let index = 0; index < MAX_TURN_BLOCKS + 30; index += 1) {
    boundedHistoryEvents.push(event({
      type: "process", sid: boundedHistorySid, item_id: `history-process-${index}`,
      kind: "hook", phase: "end", status: "succeeded", title: `History ${index}`,
    }));
  }
  boundedHistoryEvents.push(event({
    type: "turn_end", sid: boundedHistorySid,
    result: { subtype: "success", duration_ms: 1, is_error: false },
  }));
  state = reduce(state, { type: "event", event: event({
    type: "history", sid: boundedHistorySid, session_id: boundedHistorySid,
    in_progress: false, has_more: false, events: boundedHistoryEvents,
  }) });
  const boundedHistoryTurn = state.runtimes[boundedHistorySid].turns[0];
  assert.equal(boundedHistoryTurn.blocks.length, MAX_TURN_BLOCKS);
  assert.equal(boundedHistoryTurn.blocks.filter((block: {
    kind: string; item_id?: string;
  }) => block.kind === "process" && block.item_id === OMITTED_PROCESS_ITEM_ID).length, 1);
  assert.ok(boundedHistoryTurn.blocks.some((block: {
    kind: string; message_id?: string;
  }) => block.kind === "text" && block.message_id === "history-final"));

  assert.deepEqual(finalTextBlocks(richTurn.blocks).map((block: { text: string }) => block.text),
    ["已经完成。"]);
  const { presentTool, isToolFailure } = await reducerHarness.ssrLoadModule(
    "/src/tool-presentation.ts");
  assert.deepEqual(presentTool({
    kind: "tool", message_id: "m", tool_use_id: "mcp", tool: "lookup",
    input: {}, category: "mcp", title: "docs · lookup", done: false,
  }), { icon: "term", title: "docs · lookup", subtitle: "", group: "MCP" });
  assert.equal(presentTool({
    kind: "tool", message_id: "w", tool_use_id: "web", tool: "webSearch",
    input: { query: "sdk" }, category: "web_search", title: "搜索 sdk", done: false,
  }).icon, "research");
  assert.equal(isToolFailure({
    kind: "tool", message_id: "d", tool_use_id: "declined", tool: "shell",
    input: {}, done: true,
    result: { content: "", is_error: false, status: "declined" },
  }), true);

  const { ChatView } = await reducerHarness.ssrLoadModule(
    "/src/components/ChatView.tsx");
  const richMarkup = renderToStaticMarkup(createElement(ChatView, {
    sid: richSid, turns: [richTurn], engine: "codex",
    onEdit: () => {}, onGetDiff: () => {},
  }));
  assert.match(richMarkup, /已处理 3s/);
  assert.match(richMarkup, /已经完成/);
  assert.doesNotMatch(richMarkup, /先检查代码/);
  const { ProcessTimeline } = await reducerHarness.ssrLoadModule(
    "/src/components/ProcessTimeline.tsx");
  const declinedMarkup = renderToStaticMarkup(createElement(ProcessTimeline, {
    blocks: [{ kind: "process", item_id: "approval-denied", processKind: "hook",
      phase: "end", status: "declined", title: "Hook 已拒绝", done: false }],
    done: true,
  }));
  assert.match(declinedMarkup, /process-declined/);
  assert.match(declinedMarkup, /M18 6L6 18M6 6l12 12/);

  // A background task can be consumed after ResultMessage, when a new turn is
  // already open. Its authoritative engine turn id must route it back to the
  // old turn instead of creating a phantom third turn or attaching to the tail.
  state = reduce(state, { type: "query_sent", sid: richSid, prompt: "下一问",
    msg_id: "rich-next", ts: 34_000 });
  state = reduce(state, { type: "event", event: event({
    type: "process", sid: richSid, item_id: "late-agent", kind: "agent",
    phase: "end", status: "succeeded", turn_id: "turn-rich",
    title: "后台代理完成",
  }) });
  assert.equal(state.runtimes[richSid].turns.length, 2);
  assert.ok(state.runtimes[richSid].turns[0].blocks.some(
    (block: { kind: string; item_id?: string }) => block.kind === "process"
      && block.item_id === "late-agent"));
  assert.ok(!state.runtimes[richSid].turns[1].blocks.some(
    (block: { kind: string; item_id?: string }) => block.item_id === "late-agent"));

  // A late background update reopens only the process shell, not the completed
  // answer turn. The user can keep reading the final answer while the activity
  // header truthfully reports that work is still running.
  state = reduce(state, { type: "event", event: event({
    type: "process", sid: richSid, item_id: "late-agent", kind: "agent",
    phase: "update", status: "running", turn_id: "turn-rich",
    title: "后台代理运行中", progress: "继续检查",
  }) });
  const backgroundTurn = state.runtimes[richSid].turns[0];
  assert.equal(backgroundTurn.done, true);
  assert.equal(hasActiveProcess(backgroundTurn.blocks), true);
  const backgroundMarkup = renderToStaticMarkup(createElement(ChatView, {
    sid: richSid, turns: [backgroundTurn], engine: "codex",
    onEdit: () => {}, onGetDiff: () => {},
  }));
  assert.match(backgroundMarkup, /正在处理/);
  assert.match(backgroundMarkup, /继续检查/);
  state = reduce(state, { type: "event", event: event({
    type: "process", sid: richSid, item_id: "late-agent", kind: "agent",
    phase: "end", status: "succeeded", turn_id: "turn-rich",
    title: "后台代理完成",
  }) });
  assert.equal(hasActiveProcess(state.runtimes[richSid].turns[0].blocks), false);

  const forkableTurn = {
    id: "message-a", forkPointId: "codex-turn-a", prompt: "在？",
    done: true, doneTs: 12_000,
    blocks: [{ kind: "text", message_id: "answer-a", text: "在的", done: true }],
  };
  const codexMarkup = renderToStaticMarkup(createElement(ChatView, {
    sid, turns: [forkableTurn], engine: "codex",
    onEdit: () => {}, onGetDiff: () => {}, onFork: () => {},
  }));
  assert.match(codexMarkup, /aria-label="复制"/);
  assert.match(codexMarkup, /aria-label="派生"/);
  assert.match(codexMarkup, /data-tooltip="从此回复派生新会话"/);
  assert.doesNotMatch(codexMarkup, /title="从此回复派生/);
  assert.ok(codexMarkup.indexOf('aria-label="派生"')
    > codexMarkup.indexOf('aria-label="复制"'));
  const claudeMarkup = renderToStaticMarkup(createElement(ChatView, {
    sid, turns: [forkableTurn], engine: "claude",
    onEdit: () => {}, onGetDiff: () => {}, onFork: () => {},
  }));
  assert.match(claudeMarkup, /aria-label="派生"/);
  assert.match(claudeMarkup, /data-tooltip="从此回复派生新会话"/);
  const noForkPointMarkup = renderToStaticMarkup(createElement(ChatView, {
    sid, turns: [{ ...forkableTurn, forkPointId: undefined }], engine: "claude",
    onEdit: () => {}, onGetDiff: () => {}, onFork: () => {},
  }));
  assert.doesNotMatch(noForkPointMarkup, /aria-label="派生"/);

  state = reduce(state, { type: "event", event: event({
    type: "takeover_state", sid, pending: true, message: "等待当前回复结束",
  }) });
  assert.equal(state.runtimes[sid].takeoverPending, true);
  assert.equal(state.runtimes[sid].takeoverMessage, "等待当前回复结束");
  state = reduce(state, { type: "event", event: event({
    type: "takeover_state", sid, pending: false,
  }) });
  assert.equal(state.runtimes[sid].takeoverPending, false);
  assert.equal(state.runtimes[sid].takeoverMessage, null);

  const progressSid = "progress";
  state = {
    ...state,
    focusedSid: progressSid,
    runtimes: {
      ...state.runtimes,
      [progressSid]: { ...createRuntime(), state: "running", syncReady: true },
    },
  };
  state = reduce(state, {
    type: "query_sent", sid: progressSid, prompt: "status",
    msg_id: "progress-turn", ts: 20_000,
  });
  state = reduce(state, { type: "event", event: event({
    type: "state", sid: progressSid, msg_id: "progress-turn",
    state: "running", detail: "上游服务暂时不可用（503），Codex 正在重试…",
    phase: "retrying",
  }) });
  assert.equal(state.runtimes[progressSid].turns[0].done, false);
  assert.match(state.runtimes[progressSid].turns[0].progress ?? "", /503/);
  state = reduce(state, { type: "event", event: event({
    type: "state", sid: progressSid, msg_id: "progress-turn",
    state: "running", detail: null, phase: null,
  }) });
  assert.equal(state.runtimes[progressSid].turns[0].done, false);
  assert.equal(state.runtimes[progressSid].turns[0].progress, undefined);
  state = reduce(state, { type: "event", event: event({
    type: "state", sid: progressSid, msg_id: "progress-turn",
    state: "running", detail: "上游服务暂时不可用（503），Codex 正在重试…",
    phase: "retrying",
  }) });
  state = reduce(state, { type: "event", event: event({
    type: "error", sid: progressSid, msg_id: "progress-turn",
    code: "cc_crash", message: "Codex 没有返回任何内容",
  }) });
  assert.equal(state.runtimes[progressSid].turns[0].done, true);
  assert.equal(state.runtimes[progressSid].turns[0].progress, undefined);
  assert.match(state.runtimes[progressSid].turns[0].error ?? "", /没有返回任何内容/);
  state = reduce(state, { type: "event", event: event({
    type: "turn_end", sid: progressSid, ts: 21,
    result: { subtype: "error", duration_ms: 237252, is_error: true },
  }) });
  assert.equal(state.runtimes[progressSid].state, "idle");

  const { CommandSheet } = await reducerHarness.ssrLoadModule(
    "/src/components/CommandSheet.tsx");
  const picked: string[] = [];
  const tree = CommandSheet({
    open: true, kind: "models", engine: "codex", onClose: () => {},
    onPickModel: (model: string) => { picked.push(model); },
  });
  const clickModelButtons = (node: unknown): void => {
    if (Array.isArray(node)) {
      node.forEach(clickModelButtons);
      return;
    }
    if (!node || typeof node !== "object") return;
    const element = node as {
      type?: unknown; key?: string | null;
      props?: { onClick?: () => void; children?: unknown };
    };
    if (element.type === "button"
        && ["gpt-5.6-terra", "gpt-5.6-luna"].includes(element.key ?? "")) {
      element.props?.onClick?.();
    }
    clickModelButtons(element.props?.children);
  };
  clickModelButtons(tree);
  assert.deepEqual(picked, ["gpt-5.6-terra", "gpt-5.6-luna"]);
} finally {
  await reducerHarness.close();
}

const fakePng = new Uint8Array(24);
fakePng.set([0x00, ...new TextEncoder().encode("PNG\r\n\x1a\n")], 0);
fakePng.set(new TextEncoder().encode("IHDR"), 12);
new DataView(fakePng.buffer).setUint32(16, 1);
new DataView(fakePng.buffer).setUint32(20, 1);
assert.equal(imageDimensions(fakePng, "image/png"), null);

// Keep the public RelayWs return contract checked without instantiating a
// browser WebSocket in this zero-network Node test.
const btwWs: Pick<RelayWs, "sendOpenBtw"> = {
  sendOpenBtw: (_parentSid, requestId = "generated-request") => requestId,
};
const btwRequestId = btwWs.sendOpenBtw("parent-1", "btw-request-1");
assert.equal(btwRequestId, "btw-request-1");
const openFrame = makeOpenBtwCommand("parent-1", btwRequestId, 123);
assert.equal(openFrame.type, "open_btw");
assert.equal(openFrame.sid, "parent-1");
assert.equal(openFrame.request_id, btwRequestId);
assert.equal(openFrame.ts, 123);

assert.equal(matchesBtwRequest("btw-request-1", "btw-request-1"), true);
assert.equal(matchesBtwRequest("btw-request-new", "btw-request-old"), false);
assert.equal(matchesBtwRequest(null, "btw-request-old"), false);
assert.equal(classifyBtwOpened(
  "btw-request-1", null,
  { request_id: "btw-request-1", btw_sid: "btw-1" }), "accept");
assert.equal(classifyBtwOpened(
  null, { requestId: "btw-request-1", sid: "btw-1" },
  { request_id: "btw-request-1", btw_sid: "btw-1" }), "duplicate");
assert.equal(classifyBtwOpened(
  "btw-request-new", null,
  { request_id: "btw-request-old", btw_sid: "btw-old" }), "stale");
const discardedBtwSids = new Set(["btw-stale"]);
assert.equal(consumeDiscardedBtwSnapshot(
  discardedBtwSids, { sid: "normal-session" }), false);
assert.equal(discardedBtwSids.has("btw-stale"), true);
assert.equal(consumeDiscardedBtwSnapshot(
  discardedBtwSids, { sid: "btw-stale" }), true);
assert.equal(discardedBtwSids.size, 0);

const boundedCache = boundCachedTurns(Array.from(
  { length: 120 }, (_, id) => ({ id, prompt: `turn-${id}` })));
assert.equal(boundedCache.length, 100);
assert.equal((boundedCache[0] as { id: number }).id, 20);
const skipsOneOversizedCacheTurn = boundCachedTurns([
  { id: "small", prompt: "keep" },
  { id: "huge", image: "x".repeat(2 * 1024 * 1024 + 1) },
]);
assert.deepEqual(skipsOneOversizedCacheTurn, [{ id: "small", prompt: "keep" }]);
const stripsFileBodiesFromCache = boundCachedTurns([{
  id: "file-turn",
  prompt: "upload",
  files: [{ filename: "secret.txt", data: "do-not-persist", extra: "drop-me" }],
}]);
assert.deepEqual(stripsFileBodiesFromCache, [{
  id: "file-turn",
  prompt: "upload",
  files: [{ filename: "secret.txt", data: "" }],
}]);

const boundedRuntimeTurns = boundRuntimeTurns(Array.from(
  { length: 5 }, (_, id) => ({ id: `turn-${id}`, prompt: "x", blocks: [], done: true })),
3, 10_000);
assert.deepEqual(boundedRuntimeTurns.map((turn) => turn.id), ["turn-2", "turn-3", "turn-4"]);
const activeTurn = { id: "active", prompt: "running", blocks: [], done: false };
const keepsActiveRuntimeTurns = boundRuntimeTurns([
  { id: "old", prompt: "old", blocks: [], done: true },
  activeTurn,
  { id: "newest-done", prompt: "new", blocks: [], done: true },
], 2, 10_000);
assert.deepEqual(keepsActiveRuntimeTurns.map((turn) => turn.id), ["active", "newest-done"]);
const keepsNewestOversizedTurn = boundRuntimeTurns([
  { id: "older", prompt: "a".repeat(100), blocks: [], done: true },
  { id: "newer", prompt: "b".repeat(100), blocks: [], done: true },
], 10, 10);
assert.deepEqual(keepsNewestOversizedTurn.map((turn) => turn.id), ["newer"]);
const completedAnswerWithBackgroundWork = {
  id: "background-active", prompt: "done", done: true,
  blocks: [{ kind: "process" as const, item_id: "agent-1", title: "代理",
    done: false }],
};
const keepsCompletedAnswerWithBackgroundWork = boundRuntimeTurns([
  completedAnswerWithBackgroundWork,
  { id: "newer-complete", prompt: "new", blocks: [], done: true },
], 1, 10_000);
assert.deepEqual(keepsCompletedAnswerWithBackgroundWork.map((turn) => turn.id), [
  "background-active",
]);

const idleRuntime = () => ({
  state: "idle", syncReady: true, replaying: false,
  turns: [] as Array<{ done: boolean }>, queue: [] as unknown[],
  pendingSend: null, pendingQuestion: null,
});
const prunedRuntimes = pruneRuntimeMap({
  protected: idleRuntime(), oldestIdle: idleRuntime(), newestIdle: idleRuntime(),
}, new Set(["protected"]), 2);
assert.deepEqual(Object.keys(prunedRuntimes), ["protected", "newestIdle"]);
const activeRuntime = { ...idleRuntime(), state: "running" };
const keepsConfirmedActive = pruneRuntimeMap({
  protected: idleRuntime(), active: activeRuntime,
}, new Set(["protected"]), 1);
assert.deepEqual(Object.keys(keepsConfirmedActive), ["protected", "active"]);
const backgroundRuntime = {
  ...idleRuntime(), turns: [completedAnswerWithBackgroundWork],
};
const keepsBackgroundRuntime = pruneRuntimeMap({
  protected: idleRuntime(), background: backgroundRuntime,
}, new Set(["protected"]), 1);
assert.deepEqual(Object.keys(keepsBackgroundRuntime), ["protected", "background"]);
const staleActive = { ...activeRuntime, syncReady: false };
const dropsUnconfirmedOldGeneration = pruneRuntimeMap({
  protected: idleRuntime(), stale: staleActive,
}, new Set(["protected"]), 1);
assert.deepEqual(Object.keys(dropsUnconfirmedOldGeneration), ["protected"]);

// Exercise the actual WebSocket reducer boundary: live delivery can race ahead
// of Hello replay, and a cached command response can arrive after newer state.
class FakeWebSocket {
  static readonly OPEN = 1;
  static readonly instances: FakeWebSocket[] = [];

  readonly sent: string[] = [];
  readyState = FakeWebSocket.OPEN;
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onclose: ((event: { code: number }) => void) | null = null;
  onerror: (() => void) | null = null;

  constructor(readonly url: string) {
    FakeWebSocket.instances.push(this);
  }

  send(raw: string): void {
    this.sent.push(raw);
  }

  close(): void {
    this.readyState = 3;
  }

  receive(frame: Record<string, unknown>): void {
    this.onmessage?.({ data: JSON.stringify({ v: 9, ts: 1, ...frame }) });
  }
}

Object.assign(globalThis, {
  window: {
    location: { protocol: "http:", host: "relay.test", reload: () => {} },
  },
  WebSocket: FakeWebSocket,
});

const observed: ServerEvent[] = [];
let wrapperGenerationChanges = 0;
const relay = new RelayWs({
  onEvent: (event) => { observed.push(event); },
  onConnState: () => {},
  onWrapperGenerationChanged: () => { wrapperGenerationChanges += 1; },
});
relay.start();
const socket = FakeWebSocket.instances.at(-1);
assert.ok(socket);
socket.onopen?.();

const codexModels = modelsFor("codex");
assert.equal(clientSlashesFor("codex").has("plan"), true);
assert.equal(clientSlashesFor("codex").has("normal"), true);
assert.equal(commandsFor("codex").some((command) => (
  "slash" in command && command.slash === "plan"
)), true);
assert.deepEqual(matchCommands("pla", "codex").map((command) => command.slash), ["plan"]);
for (const id of ["gpt-5.6-terra", "gpt-5.6-luna"]) {
  const selected = codexModels.find((model) => model.id === id);
  assert.ok(selected);
  relay.setFocusedSid("codex-model-session", "codex");
  relay.sendSetModel(selected.id);
  const frame = JSON.parse(socket.sent.at(-1) ?? "{}");
  assert.equal(frame.type, "set_model");
  assert.equal(frame.sid, "codex-model-session");
  assert.equal(frame.model, id);
}

relay.setFocusedSid("codex-plan-session", "codex");
relay.sendSetCollaborationMode("plan");
const collaborationFrame = JSON.parse(socket.sent.at(-1) ?? "{}");
assert.equal(collaborationFrame.type, "set_collaboration_mode");
assert.equal(collaborationFrame.sid, "codex-plan-session");
assert.equal(collaborationFrame.mode, "plan");
assert.equal(typeof collaborationFrame.cmd_id, "string");
assert.equal(typeof collaborationFrame.client_id, "string");

relay.sendNewSession(
  "/tmp/project", "codex", "gpt-5.6-sol", "xhigh",
  { prompt: "先制定计划", msg_id: "first-plan-message" }, "plan",
  "on-request", "fast",
);
const newPlanFrame = JSON.parse(socket.sent.at(-1) ?? "{}");
assert.equal(newPlanFrame.type, "new_session");
assert.equal(newPlanFrame.engine, "codex");
assert.equal(newPlanFrame.collaboration_mode, "plan");
assert.equal(newPlanFrame.permission_mode, "on-request");
assert.equal(newPlanFrame.service_tier, "fast");
assert.equal(newPlanFrame.prompt, "先制定计划");

relay.sendNewSession(
  "/tmp/project", "claude", null, null,
  { prompt: "使用本机默认设置", msg_id: "default-settings-message" },
);
const defaultSessionFrame = JSON.parse(socket.sent.at(-1) ?? "{}");
assert.equal(defaultSessionFrame.type, "new_session");
assert.equal("model" in defaultSessionFrame, false);
assert.equal("effort" in defaultSessionFrame, false);

relay.sendGetModels("claude", "/tmp/project");
const claudeDefaultsFrame = JSON.parse(socket.sent.at(-1) ?? "{}");
assert.equal(claudeDefaultsFrame.type, "get_models");
assert.equal(claudeDefaultsFrame.engine, "claude");
assert.equal(claudeDefaultsFrame.cwd, "/tmp/project");
assert.equal(typeof claudeDefaultsFrame.cmd_id, "string");
assert.equal(typeof claudeDefaultsFrame.client_id, "string");

const appSource = readFileSync(resolve(process.cwd(), "src/App.tsx"), "utf8");
for (const optimisticAction of ["set_model", "set_effort", "set_perm", "set_collaboration_mode"]) {
  assert.doesNotMatch(appSource, new RegExp(`dispatch\\(\\{ type: ["']${optimisticAction}["']`));
}
assert.match(appSource, /const \{ cwd, model, effort \} = state\.newChat/);
assert.match(appSource, /data-lock-horizontal-swipe/);

assert.equal(relay.sendTakeover("codex-model-session"), true);
const takeoverFrame = JSON.parse(socket.sent.at(-1) ?? "{}");
assert.equal(takeoverFrame.type, "takeover");
assert.equal(takeoverFrame.sid, "codex-model-session");
assert.equal(typeof takeoverFrame.cmd_id, "string");
assert.equal(typeof takeoverFrame.client_id, "string");

// Stale/user-edited localStorage values must never reach strict Pydantic
// command literals. The App applies these guards before listing sessions or
// requesting a themed diff.
assert.equal(normalizeEngine("codex"), "codex");
assert.equal(normalizeEngine("legacy-engine"), "claude");
assert.equal(normalizeEngine(null), "claude");
assert.equal(normalizeDiffTheme("dark"), "dark");
assert.equal(normalizeDiffTheme("sepia"), "light");
assert.equal(normalizeDiffTheme(null), "light");

// These one-shot commands are just as reliable as chat mutations: each must
// carry the outbox identity needed for reconnect replay and wrapper dedupe.
relay.setFocusedSid("codex-control-session", "codex");
const reliableControlFrames: Array<[string, () => void]> = [
  ["get_status", () => relay.sendGetStatus()],
  ["get_goal", () => relay.sendGetGoal()],
  ["set_goal", () => relay.sendSetGoal("ship it", "active", 1024)],
  ["clear_goal", () => relay.sendClearGoal()],
  ["get_diff", () => relay.sendGetDiff("src/App.tsx", "dark")],
];
for (const [type, sendCommand] of reliableControlFrames) {
  sendCommand();
  const frame = JSON.parse(socket.sent.at(-1) ?? "{}");
  assert.equal(frame.type, type);
  assert.equal(frame.sid, "codex-control-session");
  assert.equal(typeof frame.cmd_id, "string");
  assert.equal(typeof frame.client_id, "string");
}

socket.receive({
  type: "snapshot", sid: "s1", cc_session_id: "s1", generation: "g1",
  state: "running", tail_text: "",
});
socket.receive({ type: "delta", sid: "s1", seq: 1, message_id: "m1", text: "X" });
socket.receive({
  type: "replay_start", sid: "s1", generation: "g1", from_seq: 1,
  to_seq: 2, truncated: false, rebuild: false,
});
socket.receive({ type: "delta", sid: "s1", seq: 1, message_id: "m1", text: "X" });
socket.receive({ type: "model", sid: "s1", seq: 2, model: "new" });
socket.receive({ type: "replay_end", sid: "s1", to_seq: 2, truncated: false });
socket.receive({ type: "model", sid: "s1", seq: 1, model: "old" });

assert.deepEqual(
  observed.filter((event) => event.type === "delta").map((event) => event.text),
  ["X"],
);
assert.deepEqual(
  observed.filter((event) => event.type === "model").map((event) => event.model),
  ["new"],
);

// Rebuild deliberately resets the seq epoch, so lower body frames must survive;
// once ReplayEnd closes it, the ordinary duplicate gate applies again.
socket.receive({ type: "delta", sid: "s1", seq: 10, message_id: "old", text: "old" });
socket.receive({
  type: "replay_start", sid: "s1", generation: "g1", from_seq: 1,
  to_seq: 1, truncated: false, rebuild: true,
});
socket.receive({
  type: "delta", sid: "s1", seq: 1, message_id: "rebuilt", text: "rebuilt",
});
socket.receive({ type: "replay_end", sid: "s1", to_seq: 1, truncated: false });
socket.receive({ type: "delta", sid: "s1", seq: 1, message_id: "stale", text: "stale" });
assert.equal(
  observed.filter((event) => event.type === "delta" && event.text === "rebuilt").length,
  1,
);
assert.equal(
  observed.filter((event) => event.type === "delta" && event.text === "stale").length,
  0,
);
assert.equal(relay.lastSeqFor("s1"), 1);

// The same numeric seq belongs to a different cursor domain after generation
// change and must not be mistaken for a duplicate.
// BtwOpened deliberately has no generation. Losing its following Snapshot used
// to leave the old fork stuck open because a normal session's g1 -> g2 change
// only notified the App when a per-btw generation had already been recorded.
socket.receive({
  type: "btw_opened", request_id: "btw-gap", btw_sid: "btw-old",
  parent_sid: "s1", engine: "claude",
});
socket.receive({
  type: "snapshot", sid: "s1", cc_session_id: "s1", generation: "g2",
  state: "running", tail_text: "",
});
socket.receive({ type: "model", sid: "s1", seq: 1, model: "next-generation" });
assert.equal(
  observed.filter((event) => event.type === "model"
    && event.model === "next-generation").length,
  1,
);
assert.equal(wrapperGenerationChanges, 1);
socket.receive({
  type: "snapshot", sid: "s2", cc_session_id: "s2", generation: "g2",
  state: "idle", tail_text: "",
});
assert.equal(wrapperGenerationChanges, 1); // one notice per wrapper generation
relay.stop();

console.log("web reliability tests passed");
