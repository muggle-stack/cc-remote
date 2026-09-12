import assert from "node:assert/strict";

import {
  CommandOutbox,
  QueryAcceptanceLatch,
  planRecoveryReplay,
} from "../src/outbox.ts";
import { shouldAcceptSessionList } from "../src/session-list.ts";

const outbox = new CommandOutbox(2, 4096);
const first = outbox.enqueue(
  { v: 10, type: "query", prompt: "one", ts: 1 }, "client-1", "cmd-1");
const second = outbox.enqueue(
  { v: 10, type: "interrupt", ts: 2 }, "client-1", "cmd-2");
assert.equal(first.ok, true);
assert.equal(second.ok, true);
assert.equal(outbox.size, 2);
assert.deepEqual(
  outbox.pendingFrames().map((raw) => JSON.parse(raw).cmd_id),
  ["cmd-1", "cmd-2"],
);

const full = outbox.enqueue(
  { v: 10, type: "set_model", model: "x", ts: 3 }, "client-1", "cmd-3");
assert.equal(full.ok, false);
assert.equal(outbox.size, 2); // never evict an unacknowledged older command

assert.equal(outbox.ack("other-client", "cmd-1"), false);
assert.equal(outbox.ack("client-1", "missing"), false);
assert.equal(outbox.size, 2);
assert.equal(outbox.ack("client-1", "cmd-1"), true);
assert.equal(outbox.size, 1);
assert.deepEqual(
  outbox.pendingFrames().map((raw) => JSON.parse(raw).cmd_id),
  ["cmd-2"],
);

const byteBounded = new CommandOutbox(10, 8);
assert.equal(byteBounded.enqueue(
  { v: 10, type: "query", prompt: "too large", ts: 1 }, "c", "x").ok, false);
assert.equal(byteBounded.size, 0);
assert.equal(byteBounded.byteSize, 0);

const frameBounded = new CommandOutbox(10, 4096, 160);
const smallFrame = frameBounded.enqueue(
  { v: 10, type: "query", prompt: "small", ts: 1 }, "client", "small");
assert.equal(smallFrame.ok, true);
const oversizedFrame = frameBounded.enqueue(
  { v: 10, type: "query", prompt: "x".repeat(200), ts: 1 }, "client", "oversized");
assert.equal(oversizedFrame.ok, false);
assert.match(oversizedFrame.ok ? "" : oversizedFrame.reason, /command too large/);
assert.equal(frameBounded.size, 1); // reject before mutating aggregate accounting
assert.equal(frameBounded.byteSize, smallFrame.ok ? new TextEncoder().encode(smallFrame.raw).byteLength : -1);

const rekeyed = new CommandOutbox(10, 4096);
assert.equal(rekeyed.enqueue(
  { v: 10, type: "query", sid: "tmp-old", prompt: "x", msg_id: "m", ts: 1 },
  "c", "rekey-cmd").ok, true);
assert.deepEqual(rekeyed.pendingSessionIds(), ["tmp-old"]);
assert.deepEqual(rekeyed.pendingFramesWithSessionIds().map((item) => ({
  cmd_id: JSON.parse(item.raw).cmd_id,
  sid: item.sid,
})), [{ cmd_id: "rekey-cmd", sid: "tmp-old" }]);
rekeyed.rekeySession("tmp-old", "real-id");
assert.deepEqual(rekeyed.pendingSessionIds(), ["real-id"]);
assert.equal(rekeyed.pendingFramesWithSessionIds()[0].sid, "real-id");
assert.equal(JSON.parse(rekeyed.pendingFrames()[0]).sid, "real-id");

const protectedTargets = new CommandOutbox(10, 4096);
assert.equal(protectedTargets.enqueue(
  { v: 10, type: "query", sid: "query-target", prompt: "x", msg_id: "m", ts: 1 },
  "c", "query-cmd").ok, true);
assert.equal(protectedTargets.enqueue(
  { v: 10, type: "switch_session", session_id: "switch-target", ts: 1 },
  "c", "switch-cmd").ok, true);
assert.deepEqual(protectedTargets.pendingSessionIds(), ["query-target", "switch-target"]);

const acceptance = new QueryAcceptanceLatch();
assert.equal(acceptance.begin("session-a", "message-a"), true);
assert.equal(acceptance.begin("session-a", "message-a-duplicate"), false,
  "one session cannot have two direct queries awaiting acceptance");
assert.equal(acceptance.begin("session-b", "message-b"), true,
  "another session has an independent acceptance lane");
assert.equal(acceptance.pendingMessageId("session-a"), "message-a");

const acceptanceOutbox = new CommandOutbox(10, 4096);
assert.equal(acceptanceOutbox.enqueue({
  v: 10, type: "query", sid: "session-a", prompt: "one",
  msg_id: "message-a", ts: 1,
}, "client", "query-command").ok, true);
assert.equal(acceptanceOutbox.ack("client", "query-command"), true);
assert.equal(acceptance.pendingMessageId("session-a"), "message-a",
  "transport ACK is not authoritative query acceptance");

assert.equal(acceptance.accept({
  type: "error", sid: "session-a", msg_id: "other-message",
  code: "busy",
}), false);
assert.equal(acceptance.pendingMessageId("session-a"), "message-a",
  "an unrelated correlated error cannot release the session latch");
assert.equal(acceptance.accept({
  type: "error", sid: "session-a", msg_id: "message-a",
  code: "wrapper_offline",
}), false);
assert.equal(acceptance.pendingMessageId("session-a"), "message-a",
  "relay offline is retryable and must preserve acceptance through reconnect");

const offlineReplay = new CommandOutbox(10, 4096);
assert.equal(offlineReplay.enqueue({
  v: 10, type: "query", sid: "offline-session", prompt: "retry",
  msg_id: "offline-message", ts: 2,
}, "client", "offline-command").ok, true);
assert.equal(acceptance.begin("offline-session", "offline-message"), true);
assert.deepEqual(planRecoveryReplay(
  offlineReplay.pendingFramesWithSessionIds(), "session-b"), [
  { type: "switch", sid: "offline-session" },
  { type: "command", raw: offlineReplay.pendingFrames()[0] },
  { type: "switch", sid: "session-b" },
]);
assert.equal(acceptance.pendingMessageId("offline-session"), "offline-message");
const archiveReads = new CommandOutbox(10, 4096);
for (const type of ["get_turn_file_changes", "get_diff"]) {
  assert.equal(archiveReads.enqueue({ v: 57, type, sid: "cold-background", ts: 2,
    turn_id: "historical", revision: "immutable", engine: "codex", file: "src/code.py", offset: 64,
  }, "client", type).ok, true);
}
assert.deepEqual(planRecoveryReplay(archiveReads.pendingFramesWithSessionIds(), "foreground"), [
  ...archiveReads.pendingFrames().map((raw) => ({ type: "command", raw })),
  { type: "switch", sid: "foreground" },
], "archive file pagination and historical diffs must not resume a cold engine on replay");
assert.equal(acceptance.accept({
  type: "user_msg", sid: "offline-session", msg_id: "offline-message",
}), true);
assert.equal(acceptance.pendingMessageId("offline-session"), null);

assert.equal(acceptance.accept({
  type: "turn_binding", sid: "session-a", msg_id: "message-a",
}), true);
assert.equal(acceptance.pendingMessageId("session-a"), null);
assert.equal(acceptance.pendingMessageId("session-b"), "message-b",
  "accepting session A cannot unlock session B");
acceptance.rekeySession("session-b", "session-b-real");
assert.equal(acceptance.pendingMessageId("session-b"), null);
assert.equal(acceptance.pendingMessageId("session-b-real"), "message-b",
  "temp-session capture preserves the pending query identity");
assert.deepEqual(acceptance.pendingSessionIds(), ["session-b-real"]);

assert.deepEqual(planRecoveryReplay([
  { raw: "command-a", sid: "session-a" },
  { raw: "command-global" },
  { raw: "command-b", sid: "session-b" },
  { raw: "command-btw", sid: "btw-private" },
], "focused"), [
  { type: "switch", sid: "session-a" },
  { type: "command", raw: "command-a" },
  { type: "command", raw: "command-global" },
  { type: "switch", sid: "session-b" },
  { type: "command", raw: "command-b" },
  { type: "command", raw: "command-btw" },
  { type: "switch", sid: "focused" },
]);

assert.equal(shouldAcceptSessionList("claude", "code", {
  v: 10, type: "session_list", ts: 1, engine: "claude", sessions: [],
}), true);
assert.equal(shouldAcceptSessionList("codex", "code", {
  v: 10, type: "session_list", ts: 1, engine: "claude", sessions: [],
}), false);

{
  const replies = new QueryAcceptanceLatch();
  replies.begin("question-session", "reply-1");
  let settled = false;
  const receipt = replies.receiptFor("question-session", "reply-1")!;
  void receipt.then(() => { settled = true; });
  assert.equal(replies.receiptFor("other-session", "reply-1"), null);
  assert.equal(replies.receiptFor("question-session", "other-reply"), null);
  for (const event of [
    { type: "user_msg" as const, sid: "other-session", msg_id: "reply-1" },
    { type: "turn_binding" as const, sid: "question-session", msg_id: "older-reply" },
    { type: "error" as const, sid: "question-session", msg_id: "reply-1", code: "wrapper_offline" },
  ]) assert.equal(replies.accept(event), false);
  await Promise.resolve();
  assert.equal(settled, false, "unrelated frames and a reconnect are not acceptance");
  const error = { code: "not_steerable", message: "Codex 任务已结束，本次引导未发送。" };
  replies.accept({ type: "error", sid: "question-session", msg_id: "reply-1", ...error });
  assert.deepEqual(await receipt, { accepted: false, error }, "keep the correlated rejection reason");
  replies.begin("question-session", "reply-2");
  const retry = replies.receiptFor("question-session", "reply-2")!;
  replies.rekeySession("question-session", "native-session");
  assert.equal(replies.receiptFor("native-session", "reply-2"), retry);
  assert.equal(replies.accept({ type: "error", sid: "native-session", msg_id: "reply-1", ...error }), false);
  assert.equal(replies.accept({ type: "turn_steered", sid: "native-session", msg_id: "reply-2" }), true);
  assert.deepEqual(await retry, { accepted: true });
  replies.begin("native-session", "reply-3");
  const unknown = replies.receiptFor("native-session", "reply-3")!;
  replies.accept({ type: "error", sid: "native-session", msg_id: "reply-3",
    code: "steer_outcome_unknown", message: "still reconciling" });
  assert.equal((await unknown).accepted, false, "an unknown native outcome is never success");
}

console.log("command outbox tests passed");
