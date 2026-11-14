import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { createServer } from "vite";

import type { ServerEvent } from "../src/protocol.ts";
import { viewerPageCandidates } from "../src/viewer-page-candidates.ts";
import type { Turn } from "../src/domain/conversation.ts";
import { selectSurfaceSession } from "../src/session-order.ts";
import { scopedFocusForSessionList } from "../src/session-list.ts";
import {
  isLocalViewerUrl, isRegisteredViewerLink, matchingViewer, matchingViewers, readViewerSelections, setViewerSelection,
  viewerScopeKey, writeViewerSelections, type ViewerSelection, type ViewerSite,
} from "../src/remote-viewer.ts";

const viewerScope = { machineId: "device", space: "code", engine: "codex", sid: "parent" };
const pageTurn: Turn = { id: "page-turn", done: true, prompt: "[user link](/never/index.html)", blocks: [{
  kind: "text", message_id: "answer", done: true, text: [
    "[页面](</project/page demo/index.html>)、`viewer/test.htm`",
    "[GitHub](https://github.com/repo/index.html)",
    "[Pages](https://demo.github.io/index.html)",
    "[云端](https://example.com/index.html)",
    "`http://127.0.0.1:8000/index.html`",
    "```html\n`/example/in/code.html`\n```",
    'visualize{"path":"/output/scene.html","title":"Scene"}',
  ].join("\n"),
}, { kind: "tool", tool: "Write", tool_use_id: "write", message_id: "write", input: { file_path: "/output/created.html" },
  done: true, result: { is_error: false, content: "ok" } },
{ kind: "tool", tool: "Write", tool_use_id: "failed", message_id: "failed", input: { file_path: "/failed.html" },
  done: true, result: { is_error: true, content: "failed" } }] };
assert.deepEqual(viewerPageCandidates(pageTurn), ["/project/page demo/index.html", "viewer/test.htm",
  "http://127.0.0.1:8000/index.html", "/output/scene.html", "/output/created.html"]);
assert.deepEqual(viewerPageCandidates({ ...pageTurn, blocks: [{ kind: "text", message_id: "links", done: true,
  text: "[查看模型](http://192.168.1.20:8773/) 和 http://192.168.1.20:8773/。GitHub: https://github.com/demo/index.html",
}] }), ["http://192.168.1.20:8773/"]);
assert.deepEqual(viewerPageCandidates({ ...pageTurn, blocks: [{
  kind: "text", channel: "thinking", message_id: "plan", done: true,
  text: "考虑用 `index.html`。[候选页面](/unrelated/index.html) http://127.0.0.1:8000/",
}, {
  kind: "text", channel: "final", message_id: "answer", done: true,
  text: "目录里会有 `index.html`，实际页面在 `/project/public/film/index.html`。",
}] }), ["/project/public/film/index.html"], "reasoning and bare prose filenames are not artifacts");
assert.deepEqual(viewerPageCandidates({ ...pageTurn, blocks: [{
  kind: "text", message_id: "answer", done: true, text: "[打开页面](index.html)",
}, {
  kind: "tool", tool: "Write", tool_use_id: "write", message_id: "write", input: { file_path: "created.html" },
  done: true, result: { is_error: false, content: "ok" },
}] }), ["index.html", "created.html"], "explicit relative links and successful writes retain cwd resolution");
const viewerKey = viewerScopeKey(viewerScope);
const viewerChoice = { machine_id: "resource-device", site_id: "robot" };
assert.notEqual(viewerKey, viewerScopeKey({ ...viewerScope, machineId: "another-device" }));
assert.notEqual(viewerKey, viewerScopeKey({ ...viewerScope, sid: "another-session" }));
assert.notEqual(viewerKey, viewerScopeKey({ ...viewerScope, space: "work" }));
assert.notEqual(viewerKey, viewerScopeKey({ ...viewerScope, engine: "claude" }));
const choices = readViewerSelections({ getItem: () => JSON.stringify({
  "corrupt key": null, [viewerKey]: { ...viewerChoice, cookie: "must-not-retain", origin: "not-persisted" },
}) });
assert.deepEqual(choices, { [viewerKey]: viewerChoice });
let remembered: Record<string, ViewerSelection | null> = {};
for (let i = 0; i < 45; i++) {
  remembered = setViewerSelection(remembered, viewerScopeKey({ ...viewerScope, sid: `parent-${i}` }), viewerChoice);
}
assert.equal(Object.keys(remembered).length, 32, "live Viewer descriptors are bounded too");
remembered = setViewerSelection(remembered, viewerKey, null);
assert.equal(Object.keys(remembered).at(-1), viewerKey);
let viewerStored = "";
writeViewerSelections({ setItem: (_, value) => { viewerStored = value; } }, remembered);
assert.deepEqual(readViewerSelections({ getItem: () => viewerStored }), remembered);
assert.equal(isLocalViewerUrl("http://192.168.1.10:8000/viewer/index.html"), true);
assert.equal(isLocalViewerUrl("http://localhost:9000/viewer/index.html"), true);
assert.equal(isLocalViewerUrl("https://example.com/viewer/index.html"), false);
assert.equal(isLocalViewerUrl("http://user:secret@localhost/viewer/index.html"), false);
const site: ViewerSite = { id: "robot", label: "Robot", machine_id: "resource-device",
  revision: "a".repeat(32), entry: "/viewer/index.html", urls: ["http://localhost:9000/viewer/index.html"] };
assert.equal(matchingViewer([site], site.urls[0] + "?v=2#model"), site);
assert.equal(matchingViewer([site, { ...site, machine_id: "another-device" }], site.urls[0]), null,
  "the same localhost alias on two machines must require a choice");
assert.deepEqual(matchingViewers([site, { ...site, machine_id: "another-device" }], site.urls[0]),
  [site, { ...site, machine_id: "another-device" }]);
assert.equal(isRegisteredViewerLink(null, site.urls[0]), false, "an unknown catalog must preserve native links");
assert.equal(isRegisteredViewerLink({ enabled: false, sites: [site] }, site.urls[0]), false);
assert.equal(isRegisteredViewerLink({ enabled: true, sites: [] }, site.urls[0]), false);
assert.equal(isRegisteredViewerLink({ enabled: true, sites: [site] }, "http://192.168.1.1/admin"), false);
assert.equal(isRegisteredViewerLink({ enabled: true, sites: [site] }, site.urls[0] + "?v=2#model"), true);
assert.equal(isRegisteredViewerLink({ enabled: true, sites: [site] }, "javascript:alert(1)"), false);
assert.equal(isRegisteredViewerLink({ enabled: true, sites: [site] }, "http://user:secret@localhost:9000/viewer/index.html"), false);
assert.equal(isRegisteredViewerLink({ enabled: true, sites: [{ ...site, urls: ["https://example.com/"] }] },
  "https://example.com/"), false, "registered public URLs still retain ordinary browser navigation");

assert.equal(scopedFocusForSessionList(
  "claude-before-switch",
  [{ session_id: "claude-before-switch", engine: "claude", space: "code" }],
  "codex-user-click",
  "codex",
  "code",
), "codex-user-click",
"a late Codex list validates the synchronous Codex click, not stale Claude state");
assert.equal(scopedFocusForSessionList(
  "codex-committed",
  [{ session_id: "codex-committed", engine: "codex", space: "code" }],
  "older-bookmark",
  "codex",
  "code",
), "codex-committed",
"committed focus on the listed surface remains authoritative over its bookmark");

const recentProject = {
  session_id: "project-new",
  cwd: "/home/nancy/project",
  last_modified: "300",
};
const projectOld = {
  session_id: "project-old",
  cwd: "/home/nancy/project",
  last_modified: "200",
};
const archivedNewest = {
  session_id: "archived-newest",
  last_modified: "400",
  tag: "archived" as const,
};
assert.equal(selectSurfaceSession(
  [recentProject, projectOld, archivedNewest], "project-old")?.session_id,
"project-old",
"a surface toggle must restore the exact last-viewed session immediately");
assert.equal(selectSurfaceSession(
  [archivedNewest, projectOld, recentProject])?.session_id,
"project-new",
"without a bookmark, a surface toggle must paint its newest visible session");

const appSource = readFileSync(resolve(process.cwd(), "src/App.tsx"), "utf8");
assert.match(appSource,
  /const immediate = preserveAuthority[\s\S]{0,180}selectSurfaceSession\(cachedSessions, remembered\)/,
  "ordinary surface switches must select from the scoped cached list synchronously");
assert.match(appSource,
  /if \(immediate\) \{[\s\S]{0,500}dispatch\(\{ type: "focus_session", sid: immediate\.session_id \}\)[\s\S]{0,500}resumeListedSession\(immediate, nextEngine, nextSpace, ws\)/,
  "cached surface focus must paint and resume immediately while authority refreshes");
assert.match(appSource,
  /dispatch\(\{ type: "exit_new_chat" \}\);\s*setRestoringSurfaceScope\(focusScopeKey\)/,
  "a cold surface switch must hide the sendable default draft until its list arrives");
assert.match(appSource,
  /restoringSurfaceScope === activeScopeKey \? \([\s\S]{0,220}正在恢复会话/,
  "only the exact active machine/engine/space restore scope may show the loading gate");
assert.match(appSource,
  /!state\.newChat \|\| newChatCodexProfileMissing\s*\|\| newChatClaudeProfileMissing\s*\|\| restoringSurfaceScope === activeScopeKey/,
  "the first-message path must fail closed even if a stale composer invokes it");
assert.match(appSource,
  /if \(!current\.newChat\) \{[\s\S]{0,500}cwd: inheritedCwd \|\| "~"[\s\S]{0,500}scope === focusScopeKey \? null : scope/,
  "an authoritative empty list alone may create the scoped default New Chat draft");
assert.match(appSource,
  /const focusListedSession = useCallback\([\s\S]{0,800}didInitFocusRef\.current = true;[\s\S]{0,120}preferredSurfaceFocusRef\.current = null;[\s\S]{0,120}lastFocusBySurfaceRef\.current\[focusScopeKey\] = id;/,
  "an explicit session click must synchronously revoke pending surface restoration");
assert.match(appSource,
  /const scopedCurrentSid = scopedFocusForSessionList\([\s\S]{0,500}normalizedListedSessions\?\.some/,
  "a late list must validate focus only inside its own engine and space");

const reducerHarness = await createServer({
  root: process.cwd(),
  appType: "custom",
  logLevel: "silent",
  server: { middlewareMode: true, watch: null },
});
try {
  const { Compiler } = await reducerHarness.ssrLoadModule("/src/viewer-runner/compiler.ts");
  const base = "https://cc-remote-viewer.invalid/index.html";
  const compiler = new Compiler({}, new Set(["https://esm.sh"]), base);
  Object.assign(compiler.imports, {
    "three/": new URL("missing/", base).href,
    "three/addons/": new URL("vendor/", base).href,
    "exact": new URL("vendor/Controls.js", base).href,
  });
  assert.equal(compiler.specifier("three/addons/Controls.js", base), compiler.specifier("exact", base));
  assert.equal(compiler.specifier("./vendor/Controls.js", base), compiler.specifier("exact", base));
  for (const path of ["../outside.js", "%2e%2e/outside.js", "https://evil.example/module.js"]) {
    assert.throws(() => compiler.specifier(`three/addons/${path}`, base), /前缀范围/);
  }
  assert.throws(() => compiler.specifier("https://evil.example/module.js", base), /未登记/);
  const { initialState, reduce, createRuntime } = await reducerHarness.ssrLoadModule("/src/reducer.ts");
  {
    const { DeviceViewCache } = await reducerHarness.ssrLoadModule("/src/device-view-cache.ts");
    const cache = new DeviceViewCache();
    const sid = "same-native-session";
    const stateFor = (machine: string) => ({
      ...initialState, connState: "connected", wrapperOnline: true,
      sessions: [{ session_id: sid, engine: "codex", space: "code", summary: machine }],
      focusedSid: sid,
      runtimes: { [sid]: { ...createRuntime(), syncReady: true,
        historyRevision: "revision", historyGeneration: "generation", historyHeadKnown: true,
        pendingQuestion: { ask_id: "old-question" },
        queue: [{ msg_id: "old-query", prompt: "private queued payload" }],
        turns: [{ id: "turn", prompt: "Question", done: true, ts: 1,
          blocks: [{ kind: "text", message_id: "answer", done: true, text: machine }] }],
      } },
    });
    cache.remember("mac", "codex", "code", stateFor("Mac answer"));
    cache.remember("vps", "codex", "code", stateFor("VPS answer"));
    const view = cache.get("mac", "codex", "code");
    assert.equal(view.turns[0].blocks[0].text, "Mac answer");
    assert.equal(JSON.stringify(view).includes("private queued payload"), false);
    const restored = reduce(stateFor("VPS answer"), { type: "restore_device_view", view });
    assert.equal(restored.runtimes[sid].turns[0].blocks[0].text, "Mac answer");
    assert.equal(restored.runtimes[sid].syncReady, false);
    assert.equal(restored.wrapperOnline, false);
    assert.equal(restored.runtimes[sid].pendingQuestion, null);
    assert.deepEqual(restored.runtimes[sid].queue, []);
    cache.acceptSessionList("mac", "codex", "code", [{
      session_id: sid, engine: "codex", space: "code", summary: "Renamed Mac session",
    }]);
    assert.equal(cache.sessionLists("mac")["code:codex"][0].summary, "Renamed Mac session");
    cache.invalidateSession("mac", sid);
    assert.equal(cache.get("mac", "codex", "code"), undefined);
    assert.equal(cache.get("vps", "codex", "code").turns[0].blocks[0].text, "VPS answer");
    cache.remember("mac", "codex", "code", stateFor("Mac answer"));
    cache.acceptSessionList("mac", "codex", "code", []);
    assert.equal(cache.get("mac", "codex", "code"), undefined);
    cache.remember("mac", "codex", "code", stateFor("Mac answer"));
    cache.retainMachines(["vps"]);
    assert.equal(cache.get("mac", "codex", "code"), undefined);
    assert.ok(cache.get("vps", "codex", "code"));
    cache.clear();
    assert.equal(cache.get("vps", "codex", "code"), undefined);
    const invalidated = stateFor("Invalidated answer");
    invalidated.runtimes[sid].historyInvalidated = true;
    cache.remember("mac", "codex", "code", invalidated);
    assert.equal(cache.get("mac", "codex", "code"), undefined);
    for (let i = 0; i < 65; i++) cache.remember(`device-${i}`, "codex", "code", stateFor(String(i)));
    assert.equal(cache.get("device-0", "codex", "code"), undefined);
    assert.ok(cache.get("device-64", "codex", "code"));
  }
  {
    const preview = { file: "/tmp/README.md", sid: "claude", requestId: "preview-1", kind: "md", loading: true };
    let state = { ...initialState, focusedSid: "claude", artifact: preview };
    state = reduce(state, { type: "restore_session_list", sessions: [] });
    assert.equal(state.artifact, null, "a surface transition must hide the departing file");
    state = reduce(state, { type: "focus_session", sid: "codex" });
    state = reduce(state, { type: "event", event: { type: "file_preview", sid: "claude",
      request_id: "wrong", path: preview.file, format: "markdown", content: "stale", size: 5 } });
    assert.equal(state.retainedArtifacts.claude.loading, true, "late replies must still match the exact request");
    state = reduce(state, { type: "event", event: { type: "file_preview", sid: "claude",
      request_id: "preview-1", path: preview.file, format: "markdown", content: "# Readme", size: 8 } });
    assert.equal(state.artifact, null, "a background preview response must not open over Codex");
    state = reduce(state, { type: "set_artifact", artifact: { ...preview, sid: "codex", content: "Codex file" } });
    state = reduce(state, { type: "focus_session", sid: "claude" });
    assert.equal(state.artifact.content, "# Readme");
    state = reduce(state, { type: "focus_session", sid: "claude" });
    assert.equal(state.artifact.content, "# Readme", "reconfirming focus must preserve the preview");
    state = reduce(state, { type: "clear_artifact" });
    state = reduce(state, { type: "focus_session", sid: "codex" });
    assert.equal(state.artifact.content, "Codex file");
    state = reduce(state, { type: "focus_session", sid: "claude" });
    assert.equal(state.artifact, null, "an explicitly closed preview must stay closed");
    state = reduce(state, { type: "event", event: { type: "artifact_invalidated", session_id: "codex" } });
    state = reduce(state, { type: "focus_session", sid: "codex" });
    assert.equal(state.artifact, null, "background invalidation must discard stale file bytes");
    state = reduce(state, { type: "set_artifact", artifact: preview });
    state = reduce(state, { type: "focus_session", sid: "claude" });
    state = reduce(state, { type: "reset" });
    assert.equal(state.artifact, null);
    assert.equal(state.retainedArtifacts, undefined, "device changes must clear retained private previews");
  }
  const { ChatReadingMemory } = await reducerHarness.ssrLoadModule("/src/chat-reading-memory.ts");
  const { createHistoryBrowse } = await reducerHarness.ssrLoadModule("/src/history-browse.ts");
  const memory = new ChatReadingMemory();
  const runtime = { ...createRuntime(), historyRevision: "r1", historyGeneration: "g1" };
  const position = { turnId: "read-row", blockIndex: 1, offset: -15, turnOffset: -140,
    scrollTop: 600, sizes: {}, disclosures: {}, browsing: true };
  const projection = createHistoryBrowse({ sid: "session", scopeKey: "device:code:codex",
    revision: "r1", generation: "g1", viewId: "departed-view", basePageKey: "page",
    baseTurns: [{ id: "read-row", done: true, blocks: [], detailLoading: true }],
    hasOlder: false, olderCursor: null }).projection;
  const saved = { position, revision: "r1", generation: "g1", browse: projection };
  memory.save("device:code:codex:session", saved);
  assert.equal(memory.read("device:code:codex:session", runtime), saved);
  for (const key of ["another:code:codex:session", "device:work:codex:session", "device:code:claude:session"]) {
    assert.equal(memory.read(key, runtime), undefined, "navigation memory is surface scoped");
  }
  for (const change of [{ historyRevision: "r2" }, { historyGeneration: "g2" }, { historyInvalidated: true }]) {
    assert.equal(memory.read("device:code:codex:session", { ...runtime, ...change }), undefined,
      "obsolete authority cannot reactivate old history");
  }
  for (let i = 0; i < 5; i++) memory.save(`key-${i}`, saved);
  assert.equal(memory.read("device:code:codex:session", runtime), undefined, "retained windows are bounded");
  memory.save("key-4", null);
  assert.equal(memory.read("key-4", runtime), undefined, "bottom following removes the bookmark");
  memory.clear();
  assert.equal(memory.read("key-3", runtime), undefined, "logout clears navigation memory");
  const beforeRestore = { ...initialState, focusedSid: "session", runtimes: { session: runtime } };
  const restoreAction = { type: "restore_history_browse", projection: { ...projection, viewId: "returned-view", windowEpoch: 1 } };
  const restored = reduce(beforeRestore, restoreAction);
  assert.equal(restored.historyBrowse.viewId, "returned-view");
  assert.equal(restored.historyBrowse.turns[0].detailLoading, false);
  assert.equal(restored.runtimes, beforeRestore.runtimes, "restoration never overwrites live/control state");
  assert.equal(reduce({ ...beforeRestore, focusedSid: "other" }, restoreAction).historyBrowse, null);
  assert.equal(reduce({ ...beforeRestore, runtimes: { session: { ...runtime, historyRevision: "r2" } } }, restoreAction).historyBrowse, null);
  assert.equal(reduce(restored, { type: "install_history_browse_page", sid: "session",
    scopeKey: projection.scopeKey, revision: "r1", generation: "g1", viewId: "departed-view",
    windowEpoch: 0, before: null, page: { pageKey: "late", turns: [] } }), restored,
  "a response from before navigation cannot replace the restored window");
  const explicitDraftAfterMissingFocus = reduce({
    ...initialState,
    sessions: [{
      session_id: "deleted-behind-draft", engine: "claude", space: "code",
    }],
    focusedSid: "deleted-behind-draft",
    newChat: {
      cwd: "/Volumes/MuggleSSD/workspace/robot-agent",
      cwdSource: "explicit" as const,
      model: null,
      effort: null,
      codexProfileId: null,
    },
  }, { type: "event", event: {
    v: 37,
    ts: 10,
    type: "session_list",
    engine: "claude",
    space: "code",
    sessions: [{
      session_id: "replacement", engine: "claude", space: "code",
    }],
  } as ServerEvent });
  assert.equal(explicitDraftAfterMissingFocus.focusedSid, null);
  assert.equal(explicitDraftAfterMissingFocus.newChat?.cwd,
    "/Volumes/MuggleSSD/workspace/robot-agent");
  assert.equal(explicitDraftAfterMissingFocus.newChat?.cwdSource, "explicit",
    "a missing former focus must not replace the user's explicit New Chat cwd");
} finally {
  await reducerHarness.close();
}
