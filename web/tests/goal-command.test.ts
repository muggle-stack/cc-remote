import assert from "node:assert/strict";
import { parseGoalCommand } from "../src/goal-command.js";

assert.deepEqual(parseGoalCommand(""), { kind: "show" });
assert.deepEqual(parseGoalCommand("   "), { kind: "show" });
assert.deepEqual(parseGoalCommand("clear"), { kind: "clear" });
assert.deepEqual(parseGoalCommand(" CLEAR "), { kind: "clear" });
assert.deepEqual(parseGoalCommand("ship the release"), {
  kind: "set", objective: "ship the release",
});

console.log("goal command tests passed");
