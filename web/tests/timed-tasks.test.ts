import assert from "node:assert/strict";
import { createServer } from "vite";
import { sanitizeHistoryPageForCache } from "../src/history-page-cache.ts";
import { mergeInitialHistory } from "../src/history-merge.ts";
import { PROTOCOL_VERSION } from "../src/protocol.ts";

const harness = await createServer({
  root: process.cwd(), appType: "custom", logLevel: "silent",
  server: { middlewareMode: true, watch: null },
});
try {
  const { createRuntime, initialState, reduce } = await harness.ssrLoadModule("/src/reducer.ts");
  const sid = "timer-session";
  const source = { task_id: "task-id", title: "每分钟测试", scheduled_at: 1000 };
  let state = { ...initialState, engine: "codex", focusedSid: sid, runtimes: { [sid]: createRuntime() } };
  const event = (body: Record<string, unknown>) => {
    state = reduce(state, { type: "event", event: { v: PROTOCOL_VERSION, ts: 1000, sid, ...body } });
  };
  event({ type: "user_msg", msg_id: "native-user", client_msg_id: "receipt-id", prompt: "测试", timed_task: source });
  event({ type: "user_msg", msg_id: "manual-user", prompt: "测试" });
  assert.deepEqual(state.runtimes[sid].turns[0].timedTask, source);
  assert.equal(state.runtimes[sid].turns[1].timedTask, undefined);
  const live = state.runtimes[sid].turns[0];
  const stale = { ...live, timedTask: undefined, done: true };
  const merged = mergeInitialHistory([stale], [live]);
  assert.deepEqual(merged[0].timedTask, source, "a late history page preserves the exact live receipt");
  const page = sanitizeHistoryPageForCache({
    pageKey: "latest", turns: [{ ...live, done: true }], isLatest: true, hasOlder: false, olderCursor: null,
  });
  assert.deepEqual(page.turns[0].timedTask, source, "message source survives browser cache");
  event({ type: "history", session_id: sid, revision: "new", detail: "summary",
    authoritative: true, events: [], turns: [{ id: "native-user", clientMsgId: "receipt-id",
      prompt: "测试", blocks: [], done: true, detailEventCount: 0, detailLoaded: false, timedTask: source }],
    has_more: false });
  assert.deepEqual(state.runtimes[sid].turns.find((turn: { id: string }) => turn.id === "native-user")?.timedTask, source);
  assert.equal(state.runtimes[sid].goal, null, "timed messages never create a Goal");
} finally {
  await harness.close();
}
console.log("timed-task receipt / history / cache checks passed");
