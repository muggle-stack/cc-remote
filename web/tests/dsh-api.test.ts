import assert from "node:assert/strict";
import { createServer } from "vite";

const harness = await createServer({ root: process.cwd(), appType: "custom", logLevel: "silent",
  server: { middlewareMode: true, watch: null } });
try {
  const { DshApi } = await harness.ssrLoadModule("/src/dsh-api.ts");
  const { dshReferenceToken } = await harness.ssrLoadModule("/src/components/DshReferences.tsx");
  const { mergeDshPages } = await harness.ssrLoadModule("/src/dsh-management-pages.ts");
  const merged = mergeDshPages([
    { id: "run", title: "Review", state: "running", data: { name_known: true, members: [{ seq: 1, sid: "dsh@child", label: "Check", state: "running" }] } },
  ], [
    { id: "run", title: "工作流", state: "completed", data: { name_known: false, members: [{ seq: 1, state: "failed" }] } },
  ], "workflows");
  assert.equal(merged[0].title, "Review");
  assert.equal(merged[0].state, "completed");
  assert.deepEqual(merged[0].data.members, [{ seq: 1, sid: "dsh@child", label: "Check", state: "failed" }],
    "older pages restore the exact child link without undoing its terminal outcome");
  const calls: { type: string; sid: string; id: string; exportId?: string; offset?: number; cancel?: boolean }[] = [];
  let id = 0;
  const transport = {
    sendReadDsh(sid: string) { const key = `read-${++id}`; calls.push({ type: "read", sid, id: key }); return key; },
    sendDownloadDsh(sid: string, exportId?: string, offset = 0, cancel = false) {
      const key = `download-${++id}`; calls.push({ type: "download", sid, id: key, exportId, offset, cancel }); return key;
    },
  };
  let current = transport;
  const api = new DshApi(() => current);
  let terminalFrames = 0, terminalLost = 0;
  const unwatch = api.watchTerminal("dsh@a", "lease", () => terminalFrames++, () => terminalLost++);
  assert.equal(api.accept({ type: "dsh_read_result", sid: "dsh@b", request_id: "lease", kind: "terminal_output", items: [] }), false);
  assert.equal(api.accept({ type: "dsh_read_result", sid: "dsh@a", request_id: "lease", kind: "terminal_output", items: [] }), true);
  assert.equal(terminalFrames, 1);
  api.reset();
  assert.equal(terminalLost, 1);
  assert.equal(api.accept({ type: "dsh_read_result", sid: "dsh@a", request_id: "lease", kind: "terminal_output", items: [] }), false);
  unwatch();
  const waiting = api.read("dsh@a", "search", { query: "needle" });
  const request = calls.at(-1)!;
  assert.equal(api.accept({ type: "dsh_read_result", sid: "dsh@b", request_id: request.id, items: [] }), false);
  assert.equal(api.accept({ type: "dsh_read_result", sid: "dsh@a", request_id: request.id, items: [{ title: "needle" }] }), true);
  assert.equal((await waiting).items[0].title, "needle");
  const aborted = new AbortController();
  const cancelled = api.read("dsh@a", "search", { signal: aborted.signal });
  aborted.abort();
  await assert.rejects(cancelled, { name: "AbortError" });
  assert.equal(api.accept({ type: "dsh_read_result", sid: "dsh@a", request_id: calls.at(-1)!.id }), false);
  const reset = api.read("dsh@a", "search");
  api.reset();
  await assert.rejects(reset, /会话已切换/);
  assert.equal(dshReferenceToken("email@example.com", 17), null);
  assert.deepEqual(dshReferenceToken('use @"a b', 9), { start: 4, end: 9, query: "a b" });
  assert.equal(dshReferenceToken('@[title](dsh-session:id)', 24), null);

  const exporting = new AbortController();
  const before = calls.length;
  const download = api.download("dsh@a", () => {}, exporting.signal);
  for (let count = 0; count < 100 && calls.length === before; count++) await new Promise(resolve => setTimeout(resolve, 5));
  const initial = calls.at(-1)!;
  assert.equal(initial.type, "download");
  current = { ...transport, sendDownloadDsh() { assert.fail("cancel must retain the original device transport"); } };
  exporting.abort();
  await assert.rejects(download, { name: "AbortError" });
  assert.equal(calls.at(-1)!.cancel, true);
  assert.equal(calls.at(-1)!.exportId, initial.id);
  current = transport;

  const finished = api.download("dsh@a", () => {}, new AbortController().signal);
  await new Promise(resolve => setTimeout(resolve, 10));
  const first = calls.at(-1)!;
  api.accept({ type: "dsh_download_chunk", sid: "dsh@a", request_id: first.id, export_id: first.id,
    offset: 0, total: 4, data: "UEsDBA==", done: true });
  const blob = await finished;
  assert.equal(blob.type, "application/zip");
  assert.deepEqual([...new Uint8Array(await blob.arrayBuffer())], [80, 75, 3, 4]);
  const { GoalApi } = await harness.ssrLoadModule("/src/goal-api.ts");
  const goalCalls: { sid: string; objective: string | null; status: string | null; budget: number | null }[] = [];
  const goals = new GoalApi(() => ({
    sendSetGoal(objective: string | null, status: string | null, budget: number | null, sid: string) {
      goalCalls.push({ sid, objective, status, budget }); return "goal-" + goalCalls.length;
    },
    sendClearGoal() { return "clear-goal"; },
  }));
  const saving = goals.save("codex-a", "ship", "active", 100000);
  assert.deepEqual(goalCalls[0], { sid: "codex-a", objective: "ship", status: "active", budget: 100000 });
  assert.equal(goals.accept({ type: "goal_state", sid: "codex-b", request_id: "goal-1" }), false);
  assert.equal(goals.accept({ type: "goal_state", sid: "codex-a", request_id: "goal-1" }), false,
    "successful projection still belongs to the reducer");
  await saving;
  const failing = goals.save("claude-a", "keep draft", "active", null);
  assert.equal(goals.accept({ type: "error", sid: "claude-a", request_id: "goal-2", message: "busy" }), true);
  await assert.rejects(failing, /busy/);
  const clearing = goals.clear("codex-a");
  goals.reset();
  await assert.rejects(clearing, /连接已切换/);

  const { terminalKey, modifyTerminalInput } = await harness.ssrLoadModule("/src/dsh-terminal-keyboard.ts");
  const normal = { ctrl: false, alt: false, shift: false };
  const terminal = { modes: { applicationCursorKeysMode: false } };
  assert.equal(terminalKey(terminal, "ArrowUp"), "\x1b[A");
  terminal.modes.applicationCursorKeysMode = true;
  assert.equal(terminalKey(terminal, "ArrowLeft"), "\x1bOD");
  assert.equal(terminalKey(terminal, "Shift+Tab"), "\x1b[Z");
  assert.equal(modifyTerminalInput("c", { ...normal, ctrl: true }), "\x03");
  assert.equal(modifyTerminalInput("[", { ...normal, ctrl: true }), "\x1b");
  assert.equal(modifyTerminalInput("\t", { ...normal, shift: true }), "\x1b[Z");
  assert.equal(modifyTerminalInput("b", { ...normal, alt: true }), "\x1bb");
  assert.equal(modifyTerminalInput("\x1bOD", { ...normal, alt: true, ctrl: true }), "\x1b[1;7D");
  assert.equal(modifyTerminalInput("中文", normal), "中文");
  console.log("DSH private reads, cancellation, transport scope, mentions and download: passed");
} finally {
  await harness.close();
}
