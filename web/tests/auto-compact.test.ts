import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { createServer } from "vite";

import {
  normalizeAutoCompactSelection,
  parseAutoCompactArgument,
  validAutoCompactThreshold,
} from "../src/auto-compact.ts";
import { clientSlashesFor } from "../src/data.ts";
import { PROTOCOL_VERSION, type ServerEvent } from "../src/protocol.ts";
import { RelayWs } from "../src/ws.ts";


assert.equal(clientSlashesFor("claude").has("autocompact"), true);
assert.equal(clientSlashesFor("codex").has("autocompact"), true,
  "Codex must intercept the shared Work command and reject it locally");
assert.deepEqual(parseAutoCompactArgument("inherit"), {
  ok: true,
  selection: { mode: "inherit", thresholdTokens: null },
});
assert.deepEqual(parseAutoCompactArgument("auto"), {
  ok: true,
  selection: { mode: "auto", thresholdTokens: null },
});
assert.deepEqual(parseAutoCompactArgument("250k"), {
  ok: true,
  selection: { mode: "custom", thresholdTokens: 250_000 },
});
assert.deepEqual(parseAutoCompactArgument("0.5m"), {
  ok: true,
  selection: { mode: "custom", thresholdTokens: 500_000 },
});
assert.equal(parseAutoCompactArgument("99999").ok, false);
assert.equal(parseAutoCompactArgument("100.0005k").ok, false);
assert.equal(validAutoCompactThreshold(100_000), true);
assert.equal(validAutoCompactThreshold(1_000_001), false);
assert.deepEqual(normalizeAutoCompactSelection("custom", null), {
  mode: "inherit", thresholdTokens: null,
});

const composerSource = readFileSync(resolve(
  process.cwd(), "src/components/Composer.tsx"), "utf8");
const contextPopoverSource = readFileSync(resolve(
  process.cwd(), "src/components/ContextPopover.tsx"), "utf8");
const cssSource = readFileSync(resolve(process.cwd(), "src/index.css"), "utf8");
const appSource = readFileSync(resolve(process.cwd(), "src/App.tsx"), "utf8");
const newChatSource = readFileSync(resolve(
  process.cwd(), "src/components/NewChatView.tsx"), "utf8");
const btwSource = readFileSync(resolve(
  process.cwd(), "src/components/BtwPanel.tsx"), "utf8");
assert.doesNotMatch(composerSource, /hint-auto-compact/,
  "autocompact must not occupy a persistent main-composer control");
assert.doesNotMatch(composerSource, /<span>自动压缩<\/span><b>/,
  "Work settings must not expose a persistent autocompact row");
assert.match(composerSource,
  /if \(slash === "autocompact"\) \{[\s\S]{0,120}setInput\("\/autocompact "\)/,
  "choosing the command suggestion must wait for an explicit send");
assert.doesNotMatch(newChatSource, /auto-compact-chip/,
  "new-chat autocompact must remain command-only");
assert.doesNotMatch(btwSource, /压缩 ·/,
  "BTW autocompact must remain command-only");
assert.match(appSource,
  /const requestContext = \(\) => \{[\s\S]{0,620}defer_context_request[\s\S]{0,420}sendContextRequestTo\(focusedSid, true\)/,
  "opening the context popover must explicitly request the native reading");
assert.doesNotMatch(appSource,
  /runtime\?\.contextRequestId\s*\|\|\s*runtime\?\.contextRefreshDeferred/,
  "reopening an idle popover must retry a previously deferred native read");
assert.match(composerSource, /contextExactReport/,
  "the popover must retain its last exact report while a native refresh loads");
assert.match(contextPopoverSource, /正在读取真实上下文/,
  "the lazy popover must expose a native-read loading state");
assert.doesNotMatch(composerSource + contextPopoverSource,
  /最近一轮|最近一次|容量未知/,
  "transcript estimates must not masquerade as the popover's real context");
assert.doesNotMatch(composerSource + contextPopoverSource, /capacity-unknown/,
  "unknown capacity must not add a permanent pseudo-context ring state");
assert.doesNotMatch(cssSource, /\.hint-ring\.capacity-unknown/,
  "removed pseudo-context states must not leave dead styling behind");
assert.match(appSource,
  /if \(eventEngine === "claude"\)[\s\S]{0,500}scheduleDeferredClaudeContextRefresh\(msg\.sid, "claude"\)/,
  "a deferred user refresh must retry after the authoritative idle boundary");
assert.match(appSource,
  /function handleSnapshot[\s\S]{0,700}e\.state === "idle"[\s\S]{0,500}scheduleDeferredClaudeContextRefresh/,
  "an idle replacement-wrapper Snapshot must also resume a deferred refresh");
assert.match(appSource,
  /msg\.code === "busy"[\s\S]{0,520}scheduleDeferredClaudeContextRefresh\([\s\S]{0,120}true\)/,
  "an idle/finalizer busy race must receive a bounded compensating retry");
assert.match(appSource,
  /const delays = \[100, 300, 750\] as const;[\s\S]{0,180}attempt >= delays\.length/,
  "finalizer catch-up must be bounded rather than poll a stuck session forever");
assert.match(appSource,
  /runtime\?\.contextRequestId[\s\S]{0,120}contextRequestLaunchesRef\.current\.has\(sid\)/,
  "context requests need a synchronous launch latch in addition to reducer state");
assert.match(appSource,
  /msg\.type === "turn_end"[\s\S]{0,300}!runtime\?\.contextRefreshDeferred[\s\S]{0,160}sendContextRequestTo\(msg\.sid, false, ws\)/,
  "TurnEnd cache reads must not overtake a deferred native refresh");
assert.match(composerSource, /if \(!ctxOpen\) p\.onContext\(\);/,
  "closing a context popover must not issue another native control request");
for (const source of [newChatSource, btwSource]) {
  assert.match(source, /command\?\.slash === "autocompact"/,
    "secondary composers must intercept autocompact before model submission");
  assert.match(source,
    /if \(!command\.args\) \{[\s\S]{0,160}setAutoCompactOpen\(true\)/,
    "a bare autocompact command must open its hidden editor");
  assert.match(source, /parseAutoCompactArgument\(command\.args\)/,
    "a parameterized autocompact command must apply directly");
}


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
}

Object.assign(globalThis, {
  window: {
    location: { protocol: "http:", host: "relay.test" },
  },
  WebSocket: FakeWebSocket,
});

const relay = new RelayWs({
  onEvent: () => {},
  onConnState: () => {},
});
relay.start();
const socket = FakeWebSocket.instances.at(-1);
assert.ok(socket);
socket.onopen?.();

relay.setFocusedSid("claude-autocompact", "claude", "code");
relay.sendGetContext();
const automaticContextFrame = JSON.parse(socket.sent.at(-1) ?? "{}");
assert.equal(automaticContextFrame.refresh, false,
  "automatic context reads must stay cache-only");
relay.sendGetContext(true);
assert.equal(JSON.parse(socket.sent.at(-1) ?? "{}").refresh, true,
  "an explicit context read must request a fresh native value");

assert.equal(relay.sendSetAutoCompact("custom", 250_000), true);
const autoCompactFrame = JSON.parse(socket.sent.at(-1) ?? "{}");
assert.equal(autoCompactFrame.type, "set_auto_compact");
assert.equal(autoCompactFrame.sid, "claude-autocompact");
assert.equal(autoCompactFrame.mode, "custom");
assert.equal(autoCompactFrame.threshold_tokens, 250_000);
assert.equal(typeof autoCompactFrame.cmd_id, "string");
assert.equal(typeof autoCompactFrame.client_id, "string");

assert.equal(relay.sendSetAutoCompactTo("btw-pinned", "auto"), true);
const btwAutoCompactFrame = JSON.parse(socket.sent.at(-1) ?? "{}");
assert.equal(btwAutoCompactFrame.type, "set_auto_compact");
assert.equal(btwAutoCompactFrame.sid, "btw-pinned");
assert.equal(btwAutoCompactFrame.mode, "auto");
assert.equal("threshold_tokens" in btwAutoCompactFrame, false);

const beforeInvalidAutoCompact = socket.sent.length;
assert.equal(relay.sendSetAutoCompact("custom", null), false);
assert.equal(relay.sendSetAutoCompactTo(
  "btw-pinned", "custom", 99_999), false);
assert.equal(socket.sent.length, beforeInvalidAutoCompact,
  "invalid custom thresholds must never enter the reliable outbox");

assert.equal(relay.sendNewSession(
  "/tmp/project", "claude", null, null,
  { prompt: "custom compact", msg_id: "autocompact-create-message" },
  undefined, undefined, undefined, undefined, undefined,
  "code", null, null,
  { mode: "custom", thresholdTokens: 250_000 },
), true);
const autoCompactSessionFrame = JSON.parse(socket.sent.at(-1) ?? "{}");
assert.equal(autoCompactSessionFrame.type, "new_session");
assert.equal(autoCompactSessionFrame.auto_compact_mode, "custom");
assert.equal(
  autoCompactSessionFrame.auto_compact_threshold_tokens, 250_000);
assert.equal(autoCompactSessionFrame.prompt, "custom compact");

const beforeInvalidAutoCompactSession = socket.sent.length;
assert.equal(relay.sendNewSession(
  "/tmp/project", "claude", null, null,
  { prompt: "invalid compact", msg_id: "invalid-autocompact-create" },
  undefined, undefined, undefined, undefined, undefined,
  "code", null, null,
  { mode: "custom", thresholdTokens: null },
), false);
assert.equal(socket.sent.length, beforeInvalidAutoCompactSession,
  "an invalid create request must not reserve focus ownership or send a frame");
relay.stop();


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
    v: PROTOCOL_VERSION,
    ts: 10,
    ...body,
  } as ServerEvent);

  const codexContextSid = "codex-applied-context";
  const codexOldReport = event({ type: "context_report", sid: codexContextSid,
    total_tokens: 142_045, max_tokens: 258_400, percentage: 55, categories: [] });
  let codexContextState = reduce({
    ...initialState,
    focusedSid: codexContextSid,
    runtimes: { [codexContextSid]: createRuntime() },
  }, { type: "event", event: codexOldReport });
  const codexSetting = event({ type: "codex_context", sid: codexContextSid,
    threshold_tokens: 400_000, applied_threshold_tokens: null, pending: true });
  codexContextState = reduce(codexContextState, { type: "event", event: codexSetting });
  assert.equal(codexContextState.runtimes[codexContextSid].contextReport, codexOldReport);
  codexContextState = reduce(codexContextState, { type: "event", event: {
    ...codexSetting, applied_threshold_tokens: 400_000, pending: false,
  } as ServerEvent });
  assert.equal(codexContextState.runtimes[codexContextSid].contextReport, null,
    "accepted Codex settings clear the previous configuration's capacity");
  const codexFreshReport = event({ ...codexOldReport, max_tokens: 400_000, percentage: 35.51 });
  codexContextState = reduce(codexContextState, { type: "event", event: codexFreshReport });
  assert.equal(codexContextState.runtimes[codexContextSid].contextReport, codexFreshReport);
  codexContextState = reduce(codexContextState, { type: "event", event: {
    ...codexSetting, threshold_tokens: null, applied_threshold_tokens: 400_000, pending: true,
  } as ServerEvent });
  assert.equal(codexContextState.runtimes[codexContextSid].contextReport, codexFreshReport,
    "a pending reset retains the still-applied capacity");

  assert.equal(createRuntime().autoCompact, null,
    "a session must not claim a mode before the wrapper reports it");
  const sid = "claude-autocompact-state";
  const state = reduce({
    ...initialState,
    focusedSid: sid,
    runtimes: { [sid]: createRuntime() },
  }, {
    type: "event",
    event: event({
      type: "auto_compact",
      sid,
      mode: "custom",
      threshold_tokens: 250_000,
      applied_mode: "inherit",
      pending: true,
      phase: "waiting_terminal",
      mutable: true,
    }),
  });
  assert.equal(state.runtimes[sid].autoCompact.mode, "custom");
  assert.equal(state.runtimes[sid].autoCompact.threshold_tokens, 250_000);
  assert.equal(state.runtimes[sid].autoCompact.applied_mode, "inherit");
  assert.equal(state.runtimes[sid].autoCompact.pending, true);
  assert.equal(state.runtimes[sid].autoCompact.phase, "waiting_terminal");

  let contextState = reduce({
    ...initialState,
    focusedSid: sid,
    runtimes: { [sid]: createRuntime() },
  }, {
    type: "begin_context_request",
    sid,
    requestId: "context-request-b",
  });
  contextState = reduce(contextState, {
    type: "event",
    event: event({
      type: "context_report",
      sid,
      request_id: "context-request-a",
      total_tokens: 120,
      max_tokens: 1_000,
      percentage: 12,
      categories: [],
    }),
  });
  assert.equal(contextState.runtimes[sid].contextReport?.total_tokens, 120,
    "every valid broadcast report should refresh the visible reading");
  assert.equal(contextState.runtimes[sid].contextRequestId, "context-request-b",
    "an older report must not settle a newer explicit context request");
  contextState = reduce(contextState, {
    type: "event",
    event: event({
      type: "context_report",
      sid,
      request_id: "context-request-b",
      total_tokens: 140,
      max_tokens: 1_000,
      percentage: 14,
      categories: [],
    }),
  });
  assert.equal(contextState.runtimes[sid].contextRequestId, null,
    "only the matching report may settle the pending context request");
  assert.equal(contextState.runtimes[sid].contextExactReport?.total_tokens, 140,
    "a successful native report must become the durable exact reading");

  contextState = reduce(contextState, {
    type: "event",
    event: event({
      type: "model",
      sid,
      model: "claude-opus-5[1m]",
    }),
  });
  assert.equal(contextState.runtimes[sid].model, "claude-opus-5[1m]");
  assert.equal(contextState.runtimes[sid].contextReport, null,
    "a model change must retire the previous model's visible context total");
  assert.equal(contextState.runtimes[sid].contextExactReport, null,
    "a model change must retire the previous model's exact breakdown");

  contextState = reduce(contextState, {
    type: "event",
    event: event({
      type: "context_report",
      sid,
      total_tokens: 160,
      max_tokens: 2_000,
      percentage: 8,
      model: "claude-opus-5[1m]",
      categories: [],
    }),
  });
  contextState = reduce(contextState, {
    type: "event",
    event: event({
      type: "model",
      sid,
      model: "claude-opus-5[1m]",
    }),
  });
  assert.equal(contextState.runtimes[sid].contextExactReport?.total_tokens, 160,
    "a repeated announcement for the report's model must retain exact context");

  contextState = reduce(contextState, {
    type: "event",
    event: event({
      type: "context_report",
      sid,
      total_tokens: 180,
      max_tokens: 1_000,
      percentage: 18,
      source: "recent_turn",
      categories: [],
    }),
  });
  assert.equal(contextState.runtimes[sid].contextReport?.total_tokens, 180,
    "the lightweight ring may track the latest turn estimate");
  assert.equal(contextState.runtimes[sid].contextExactReport?.total_tokens, 160,
    "a recent-turn estimate must not overwrite the popover's exact reading");

  contextState = reduce(contextState, {
    type: "begin_context_request",
    sid,
    requestId: "context-busy",
  });
  contextState = reduce(contextState, {
    type: "event",
    event: event({
      type: "error",
      sid,
      code: "busy",
      message: "busy",
      request_id: "context-busy",
    }),
  });
  assert.equal(contextState.runtimes[sid].contextRequestId, null);
  assert.equal(contextState.runtimes[sid].contextRefreshDeferred, true,
    "a busy native read must remain queued for the next terminal boundary");
  assert.equal(contextState.runtimes[sid].contextError, null,
    "a deferred refresh is not a user-visible failure");
  assert.equal(contextState.runtimes[sid].contextExactReport?.total_tokens, 160,
    "deferral must preserve the last exact report");

  contextState = reduce(contextState, {
    type: "event",
    event: event({
      type: "context_report",
      sid,
      total_tokens: 190,
      max_tokens: 1_000,
      percentage: 19,
      source: "recent_turn",
      categories: [],
    }),
  });
  assert.equal(contextState.runtimes[sid].contextRefreshDeferred, true,
    "a recent-turn broadcast must not consume an exact refresh intent");
  contextState = reduce(contextState, {
    type: "event",
    event: event({
      type: "context_report",
      sid,
      total_tokens: 195,
      max_tokens: 1_000,
      percentage: 19.5,
      source: "cached_control",
      categories: [],
    }),
  });
  assert.equal(contextState.runtimes[sid].contextRefreshDeferred, true,
    "a cached old-generation report must not consume an exact refresh intent");

  const deferredSatisfied = reduce(contextState, {
    type: "event",
    event: event({
      type: "context_report",
      sid,
      total_tokens: 200,
      max_tokens: 1_000,
      percentage: 20,
      source: "control",
      categories: [],
    }),
  });
  assert.equal(deferredSatisfied.runtimes[sid].contextRefreshDeferred, false,
    "another fresh native control report may satisfy the deferred intent");

  contextState = reduce(contextState, {
    type: "begin_context_request",
    sid,
    requestId: "context-failed",
  });
  contextState = reduce(contextState, {
    type: "event",
    event: event({
      type: "error",
      sid,
      code: "internal",
      message: "context unavailable",
      request_id: "context-failed",
    }),
  });
  assert.equal(contextState.runtimes[sid].contextRefreshDeferred, false);
  assert.match(contextState.runtimes[sid].contextError ?? "", /操作未完成/);
  assert.equal(contextState.runtimes[sid].contextExactReport?.total_tokens, 195,
    "a failed refresh must preserve the last exact report");

  const unrequestedContextState = reduce({
    ...initialState,
    focusedSid: sid,
    runtimes: {
      [sid]: {
        ...createRuntime(),
        contextError: "stale context error",
      },
    },
  }, {
    type: "event",
    event: event({
      type: "context_report",
      sid,
      total_tokens: 160,
      max_tokens: 1_000,
      percentage: 16,
      categories: [],
    }),
  });
  assert.equal(unrequestedContextState.runtimes[sid].contextError, null,
    "an unrequested broadcast should clear an obsolete local error");
  assert.equal(unrequestedContextState.runtimes[sid].contextReport?.total_tokens,
    160);

  const defaultNewChat = reduce(initialState, {
    type: "enter_new_chat",
    cwd: "/repo",
  });
  assert.equal(defaultNewChat.newChat?.autoCompactMode, "inherit");
  assert.equal(defaultNewChat.newChat?.autoCompactThresholdTokens, null,
    "new Claude chats must not override the native autocompact window");

  const newChat = reduce(defaultNewChat, {
    type: "set_new_chat_auto_compact",
    mode: "custom",
    thresholdTokens: 500_000,
  });
  assert.equal(newChat.newChat?.autoCompactMode, "custom");
  assert.equal(newChat.newChat?.autoCompactThresholdTokens, 500_000);
} finally {
  await reducerHarness.close();
}

console.log("Claude autocompact tests passed");
