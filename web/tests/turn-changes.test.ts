import assert from "node:assert/strict";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";
import { PROTOCOL_VERSION, type ServerEvent } from "../src/protocol.ts";
import { createHistoryBrowse } from "../src/history-browse.ts";
import { fileType, presentChangedPaths } from "../src/file-presentation.ts";
import "./turn-file-pages.test.ts";

const harness = await createServer({
  root: process.cwd(), appType: "custom", logLevel: "silent",
  server: { middlewareMode: true, watch: null },
});
try {
  const { createRuntime, initialState, reduce } = await harness.ssrLoadModule("/src/reducer.ts");
  const { ChatView } = await harness.ssrLoadModule("/src/components/ChatView.tsx");
  const { TurnChangesPanel } = await harness.ssrLoadModule("/src/components/TurnChangesPanel.tsx");
  const event = (body: Record<string, unknown>): ServerEvent => ({
    v: PROTOCOL_VERSION, ts: 10, ...body,
  } as ServerEvent);
  const changesSid = "immutable-turn-diffs";
  let changesState = { ...initialState, focusedSid: changesSid, runtimes: {
    [changesSid]: { ...createRuntime(), turns: [
      { id: "old-display", historyTurnId: "old-native", prompt: "first", blocks: [], done: true },
      { id: "new-display", historyTurnId: "new-native", prompt: "second", blocks: [], done: false },
    ] },
  } };
  const archivedChanges = { revision: "first-revision", files: [
    { path: "/repo/src/code.ts", state: "available", additions: 1, deletions: 1 },
  ] };
  changesState = reduce(changesState, { type: "event", event: event({
    type: "turn_file_changes", sid: changesSid, turn_id: "old-native", changes: archivedChanges,
  }) });
  assert.equal(changesState.runtimes[changesSid].turns[0].fileChanges?.revision, "first-revision");
  assert.equal(changesState.runtimes[changesSid].turns[0].fileChangesTurnId, "old-native");
  changesState = reduce(changesState, { type: "event", event: event({
    type: "turn_file_changes", sid: changesSid, turn_id: "new-native",
    changes: { ...archivedChanges, revision: "second-revision" },
  }) });
  assert.equal(changesState.runtimes[changesSid].turns[0].fileChanges?.revision, "first-revision",
    "the next turn touching the same path cannot replace an earlier archive reference");
  assert.equal(changesState.runtimes[changesSid].turns[1].fileChanges?.revision, "second-revision");
  changesState = reduce(changesState, { type: "event", event: event({
    type: "turn_file_changes", sid: changesSid, turn_id: "unknown-owner", changes: archivedChanges,
  }) });
  assert.equal(changesState.runtimes[changesSid].turns.length, 2,
    "an unowned sidecar cannot create or attach to a guessed visible turn");
  changesState = reduce(changesState, { type: "event", event: event({
    type: "turn_file_changes", sid: changesSid, turn_id: "new-native",
    changes: { revision: "failed-edit", files: [] },
  }) });
  assert.deepEqual(changesState.runtimes[changesSid].turns[1].fileChanges?.files, [],
    "failed edits retire their pending file list");

  const browse = createHistoryBrowse({
    scopeKey: "machine:code:codex", sid: changesSid, revision: "r1", viewId: "reading-old-page",
    baseTurns: changesState.runtimes[changesSid].turns, basePageKey: "page-1",
    hasOlder: true, olderCursor: "old-native",
  }).projection;
  assert.ok(browse);
  const reading = { ...changesState, historyBrowse: browse, retainedHistoryBrowse: browse };
  for (const kind of ["query_sent", "steer_sent", "enqueue", "set_pending"] as const) {
    let sent = reduce(reading, kind === "enqueue" || kind === "set_pending"
      ? { type: kind, sid: changesSid, query: { prompt: "keep reading", msg_id: "send" } }
      : { type: kind, sid: changesSid, prompt: "keep reading", msg_id: "send", ts: 11 });
    assert.equal(sent.historyBrowse?.viewId, browse.viewId, kind);
    assert.deepEqual(sent.historyBrowse?.turns, browse.turns, kind);
    assert.equal(sent.retainedHistoryBrowse?.viewId, browse.viewId, kind);
    sent = reduce(sent, { type: "event", event: event({
      type: "user_msg", sid: changesSid, msg_id: "send", prompt: "keep reading",
    }) });
    assert.equal(sent.historyBrowse?.viewId, browse.viewId, `${kind} echo`);
    assert.deepEqual(sent.historyBrowse?.turns, browse.turns, `${kind} echo`);
  }

  const fallbackNoticeMarkup = renderToStaticMarkup(createElement(ChatView, {
    sid: "model-fallback", engine: "claude", turns: [{ id: "fallback-turn", done: true,
      prompt: "test", blocks: [{ kind: "process", item_id: "fallback-notice", processKind: "model",
        tool: "model_refusal_fallback", phase: "snapshot", status: "succeeded", done: true,
        title: "模型已回退", summary: "claude-original → claude-fallback · 原模型拒绝响应",
      }],
    }],
  }));
  assert.match(fallbackNoticeMarkup, /claude-original.*claude-fallback/);
  assert.doesNotMatch(fallbackNoticeMarkup, /查看本轮详情|查看更多内容|class="process-timeline/,
    "a model switch stays visible without creating an empty processed disclosure");

  const legacyTurn = { id: "legacy", prompt: "edit", done: true, blocks: [{
    kind: "tool", tool: "fileChange", tool_use_id: "edit", message_id: "m", done: true,
    input: { path: "/repo/src/code.ts" },
    result: { is_error: false, content: "ok", diff: "--- src/code.ts\n+++ src/code.ts\n@@ -1 +1 @@\n-A\n+B" },
  }] };
  const legacy = renderToStaticMarkup(createElement(TurnChangesPanel, {
    turn: legacyTurn, open: true, onToggle: () => {}, work: false, onOpenLegacyDiff: () => {},
  }));
  assert.match(legacy, /turn-change-path" disabled/,
    "an uncorrelated legacy path must not open a blank or unrelated diff");
  const empty = renderToStaticMarkup(createElement(TurnChangesPanel, {
    turn: { ...legacyTurn, fileChanges: { revision: "failed", files: [] } },
    open: true, onToggle: () => {}, work: false,
  }));
  assert.equal(empty, "", "an authoritative empty result cannot resurrect legacy pending file chips");
  const paths = ["/repo/src/camera.py", "/repo/tests/camera.py", "/repo/config/rig.json", "/repo/deploy/Dockerfile", "/repo/README.md"];
  const labels = presentChangedPaths(paths);
  assert.deepEqual(labels.map((file) => file.directory), ["src", "tests", "config", "deploy", ""]);
  assert.deepEqual(labels.map((file) => file.path), paths, "path shortening is presentation only");
  assert.deepEqual(labels.map((file) => file.type.badge), ["PY", "PY", "{}", "DK", "MD"]);
  assert.equal(fileType("src/Component.TSX").label, "TypeScript");
  assert.equal(fileType("bin/no-extension").label, "文件");
  assert.equal(fileType("scripts/test.sh").badge, "$_");
  assert.equal(presentChangedPaths(["C:\\repo\\src\\one.ts"])[0].directory, "src");
  const historical = { id: "historical-files", prompt: "old changes", blocks: [], done: true,
    fileChanges: { revision: "saved", files: paths.map((path) => ({ path, state: "unavailable" })) } };
  const oldMarkup = renderToStaticMarkup(createElement(TurnChangesPanel, {
    turn: historical, open: true, onToggle: () => {}, work: false, onPreviewMarkdown: () => {},
  }));
  assert.match(oldMarkup, /改动.*5.*个文件/);
  assert.match(oldMarkup, /预览当前文件/);
  assert.match(oldMarkup, /aria-label="Python"/);
  assert.match(oldMarkup, /aria-label="JSON"/);
  assert.match(oldMarkup, /turn-change-name">camera.py/);
  assert.match(oldMarkup, /turn-change-directory">tests/);
  assert.match(oldMarkup, /aria-label="\/repo\/tests\/camera.py"/);
} finally {
  await harness.close();
}
