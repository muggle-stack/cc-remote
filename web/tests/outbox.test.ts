import assert from "node:assert/strict";

import { CommandOutbox, planRecoveryReplay } from "../src/outbox.ts";
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

assert.equal(shouldAcceptSessionList("claude", {
  v: 10, type: "session_list", ts: 1, engine: "claude", sessions: [],
}), true);
assert.equal(shouldAcceptSessionList("codex", {
  v: 10, type: "session_list", ts: 1, engine: "claude", sessions: [],
}), false);

console.log("command outbox tests passed");
