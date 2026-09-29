import assert from "node:assert/strict";
import { createServer } from "vite";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import type { Turn } from "../src/reducer.ts";

const harness = await createServer({
  root: process.cwd(), appType: "custom", logLevel: "silent",
  server: { middlewareMode: true, watch: null },
});

try {
  const { initialState, createRuntime, reduce } = await harness.ssrLoadModule("/src/reducer.ts");
  const { QueuedQueryChip } = await harness.ssrLoadModule("/src/components/QueuedQueryChip.tsx");
  const sid = "claude-session";
  const original: Turn = { id: "original-task", prompt: "keep working", done: false,
    blocks: [{ kind: "text", message_id: "response", text: "working",
      channel: "commentary", done: false }] };
  const pendingQuestion = { ask_id: "question", question: "continue?", options: [] };
  const initial = {
    ...initialState, focusedSid: sid,
    runtimes: { [sid]: { ...createRuntime(), state: "running", turns: [original],
      liveOwner: { turnId: original.id, seq: 2542 }, pendingQuestion } },
  };
  const rejection = { v: 72, type: "error", sid, ts: 1790596023.95201,
    code: "busy", message: "该会话正忙,先 interrupt", msg_id: "rejected-message" };
  const images = [{ media_type: "image/png", data: "aW1hZ2U=" }];
  const files = [{ filename: "notes.txt", data: "bm90ZXM=" }];
  const sent = reduce(initial, { type: "query_sent", sid, msg_id: rejection.msg_id,
    prompt: "new instruction", images, files, ts: 1790596023950 });

  // The same event as the production replay: another browser must not acquire
  // an empty failed turn, a notice, or a different live owner from this error.
  const observer = reduce(initial, { type: "event", event: { ...rejection, seq: 2543 } });
  assert.deepEqual(observer.runtimes[sid].turns, [original]);
  assert.deepEqual(observer.runtimes[sid].liveOwner, initial.runtimes[sid].liveOwner);
  assert.equal(observer.runtimes[sid].state, "running");
  assert.equal(observer.banner, initial.banner);

  for (const error of [
    { ...rejection, seq: 2543 }, // compatibility with the old broadcast ring
    { ...rejection, request_id: "send-command", to: "sender" },
    { ...rejection, code: "not_running", request_id: "send-command", to: "sender" },
    { ...rejection, code: "bad_prompt", request_id: "send-command", to: "sender" },
    { ...rejection, code: "internal", request_id: "send-command", to: "sender" },
  ]) {
    const rejected = reduce(sent, { type: "event", event: error });
    const rt = rejected.runtimes[sid];
    assert.deepEqual(rt.turns, [original], "unaccepted optimistic row is not engine history");
    assert.equal(rt.state, "running");
    assert.deepEqual(rt.liveOwner, initial.runtimes[sid].liveOwner);
    assert.deepEqual(rt.pendingQuestion, pendingQuestion);
    assert.equal(rt.acceptancePending, null);
    assert.equal(rt.acceptanceQuery, null);
    assert.equal(rt.failedDeferred.length, 1);
    assert.equal(rt.failedDeferred[0].prompt, "new instruction");
    assert.deepEqual(rt.failedDeferred[0].images, images);
    assert.deepEqual(rt.failedDeferred[0].files, files, "keep full attachment bytes for manual retry");
    assert.equal(rt.failedDeferred[0].queueState, "failed");
    assert.match(rt.failedDeferred[0].queueError, /未发送|无法发送/);
    assert.equal(rt.queue.length, 0, "rejection must not automatically enqueue or resubmit");
    assert.equal(rt.pendingSend, null);
    const html = renderToStaticMarkup(createElement(QueuedQueryChip, {
      query: rt.failedDeferred[0], onOpen() {}, onRemove() {},
    }));
    assert.match(html, /未发送/);
    assert.match(html, /new instruction/);
    const repeated = reduce(rejected, { type: "event", event: error });
    assert.deepEqual(repeated.runtimes[sid].failedDeferred, rt.failedDeferred);
    assert.deepEqual(repeated.runtimes[sid].turns, rt.turns);
  }

  const unrelated = reduce(sent, { type: "event", event: { ...rejection, msg_id: "other-message" } });
  assert.deepEqual(unrelated.runtimes[sid].acceptanceQuery, sent.runtimes[sid].acceptanceQuery);
  const rekeyed = reduce({ ...sent, runtimes: {
    ...sent.runtimes, "real-session": createRuntime(),
  } }, { type: "event", event: {
    v: 72, type: "session_rekey", ts: 1790596024, old_key: sid, session_id: "real-session",
  } });
  const rejectedAfterRekey = reduce(rekeyed, { type: "event", event: {
    ...rejection, sid: "real-session", to: "sender", request_id: "send-command",
  } });
  assert.deepEqual(rejectedAfterRekey.runtimes["real-session"].failedDeferred[0].files, files);

  // Exact native acceptance wins over a delayed rejection, and releases the
  // private retry payload. Real execution errors must still render as failures.
  const accepted = reduce(sent, { type: "event", event: {
    v: 72, ts: 1790596024, type: "user_msg", sid,
    msg_id: rejection.msg_id, prompt: "new instruction", seq: 2543,
  } });
  assert.equal(accepted.runtimes[sid].acceptanceQuery, null);
  const late = reduce(accepted, { type: "event", event: {
    ...rejection, request_id: "send-command", to: "sender",
  } });
  assert.deepEqual(late.runtimes[sid].turns, accepted.runtimes[sid].turns);
  assert.equal(late.runtimes[sid].failedDeferred.length, 0);
  const failed = reduce(accepted, { type: "event", event: {
    ...rejection, code: "cc_crash", message: "请求超时，请重新尝试。", seq: 2544,
  } });
  const failedTurn = failed.runtimes[sid].turns.find((t: { id: string }) => t.id === rejection.msg_id);
  assert.equal(failedTurn.done, true);
  assert.equal(failedTurn.terminalSource, "failed");
  assert.equal(failedTurn.error, "请求超时，请重新尝试。");
  console.log("query rejection tests passed");
} finally {
  await harness.close();
}
