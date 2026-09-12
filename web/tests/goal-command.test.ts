import assert from "node:assert/strict";
import { parseGoalCommand } from "../src/goal-command.js";
import { GoalApi } from "../src/goal-api.js";
import { PROTOCOL_VERSION } from "../src/protocol.js";
import type { RelayWs } from "../src/ws.js";
import { resetGoalDismissMigrationTracking } from
  "../src/scoped-goal-ui.js";

assert.deepEqual(parseGoalCommand("", "codex"), { kind: "show" });
assert.deepEqual(parseGoalCommand("   ", "claude"), { kind: "show" });
assert.deepEqual(parseGoalCommand("clear", "codex"), { kind: "clear" });
assert.deepEqual(parseGoalCommand(" CLEAR ", "claude"), { kind: "clear" });
assert.deepEqual(parseGoalCommand("resume", "codex"), { kind: "resume" });
assert.deepEqual(parseGoalCommand(" RESUME ", "codex"), { kind: "resume" });
assert.deepEqual(parseGoalCommand("resume", "claude"), {
  kind: "set", objective: "resume",
});
assert.deepEqual(parseGoalCommand(" RESUME ", "claude"), {
  kind: "set", objective: "RESUME",
});
assert.deepEqual(parseGoalCommand("resume release", "codex"), {
  kind: "set", objective: "resume release",
});
assert.deepEqual(parseGoalCommand("ship the release", "claude"), {
  kind: "set", objective: "ship the release",
});

const migrationKey = "machine\0session\0goal";
const dismissMigrations = new Set([migrationKey]);
const migrationByRequest = new Map([["replayed-request", migrationKey]]);
resetGoalDismissMigrationTracking(dismissMigrations, migrationByRequest);
assert.equal(dismissMigrations.size, 0);
assert.equal(migrationByRequest.size, 0,
  "reconnect resets both Goal-dismiss maps after reliable replay");
assert.equal(dismissMigrations.has(migrationKey), false,
  "fresh GoalState can retry a migration after the replayed request errors");
dismissMigrations.add(migrationKey);
migrationByRequest.set("fresh-request", migrationKey);
assert.equal(migrationByRequest.get("fresh-request"), migrationKey,
  "the retry establishes a fresh request correlation");

const goalCalls: unknown[][] = [];
const goalApi = new GoalApi(() => ({
  sendSetGoal(objective, status, budget, sid) {
    goalCalls.push([objective, status, budget, sid]);
    return `goal-${goalCalls.length}`;
  },
  sendClearGoal(sid) {
    goalCalls.push(["clear", sid]);
    return "clear-goal";
  },
} as RelayWs));
const saving = goalApi.save("codex-a", "ship", "active", 100000);
assert.deepEqual(goalCalls, [["ship", "active", 100000, "codex-a"]]);
const goalConfirmation = {
  v: PROTOCOL_VERSION, ts: 0, type: "goal_state" as const,
  request_id: "goal-1", sid: "codex-b", goal: null,
};
assert.equal(goalApi.accept(goalConfirmation), false,
  "another session's confirmation must not settle the pending save");
assert.equal(goalApi.accept({ ...goalConfirmation, sid: "codex-a" }), false,
  "confirmed Goal state must continue to the reducer");
await saving;

const failing = goalApi.save("claude-a", "keep draft", "active", null);
assert.equal(goalApi.accept({
  v: PROTOCOL_VERSION, ts: 0, type: "error", code: "busy",
  request_id: "goal-2", sid: "claude-a", message: "busy",
}), true, "a failed native save is handled inside the Goal dialog");
await assert.rejects(failing, /busy/);

const clearing = goalApi.clear("codex-a");
assert.deepEqual(goalCalls.at(-1), ["clear", "codex-a"]);
goalApi.reset();
await assert.rejects(clearing, /连接已切换/,
  "switching connection scope rejects pending operations without losing drafts");
assert.equal(goalApi.accept({
  ...goalConfirmation, sid: "codex-a", request_id: "clear-goal",
}), false, "a late confirmation after reset belongs to the old connection");
await assert.rejects(new GoalApi(() => null).clear("codex-a"), /连接不可用/);

console.log("goal command tests passed");
