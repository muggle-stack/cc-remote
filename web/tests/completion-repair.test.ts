import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import {
  acknowledgeCompletion,
  BtwCompletionTracker,
  completionNeedsHistoryRepair,
  idleTurnNeedsHistoryRepair,
  markCompletionUnread,
  nextTerminalHistoryRepairAttempt,
  settleTerminalHistoryRepairAttempt,
} from "../src/completion-badges.ts";

const btwCompletions = new BtwCompletionTracker();
const terminal = { turn_id: "answer-1", checkpoint_id: "question-1", ts: 100, seq: 20 };
assert.equal(btwCompletions.take("machine-a", "btw-one", terminal), true);
const unreadBtw = markCompletionUnread({}, "parent", "btw-one", "btw");
assert.deepEqual(acknowledgeCompletion(unreadBtw, "parent", { btwSid: "btw-one" }), {});
assert.equal(btwCompletions.take("machine-a", "btw-one", {
  turn_id: terminal.turn_id, checkpoint_id: terminal.checkpoint_id, ts: terminal.ts,
}), false, "reading a badge cannot make the same unsequenced history terminal new again");
assert.equal(btwCompletions.take("machine-a", "btw-one", {
  ...terminal, turn_id: "answer-2",
}), true, "a distinct follow-up answer sharing its checkpoint still notifies");
assert.equal(btwCompletions.take("machine-a", "btw-one", {
  ...terminal, checkpoint_id: "question-2",
}), true, "the exact completion identity includes its human turn");
assert.equal(btwCompletions.take("machine-a", "btw-two", terminal), true,
  "one side chat cannot consume a sibling's completion");
assert.equal(btwCompletions.take("machine-b", "btw-one", terminal), true,
  "completion identities remain device-scoped");
assert.equal(btwCompletions.take("machine-a", "btw-legacy", { ts: 120 }), true);
assert.equal(btwCompletions.take("machine-a", "btw-legacy", { ts: 120 }), false);
assert.equal(btwCompletions.take("machine-a", "btw-legacy", { ts: 121 }), true);
btwCompletions.forget("machine-a", "btw-one");
assert.equal(btwCompletions.take("machine-a", "btw-one", terminal), true);
assert.equal(btwCompletions.take("machine-a", "btw-two", terminal), false,
  "closing one BTW preserves the read state of its siblings");
btwCompletions.clear("machine-a");
assert.equal(btwCompletions.take("machine-a", "btw-two", terminal), true);
assert.equal(btwCompletions.take("machine-b", "btw-one", terminal), false,
  "a wrapper generation reset cannot forget another device's read completions");
btwCompletions.clear();
assert.equal(btwCompletions.take("machine-b", "btw-one", terminal), true);

assert.equal(completionNeedsHistoryRepair([], "native-terminal"), true,
  "a durable completion repairs an empty cold browser projection");
assert.equal(completionNeedsHistoryRepair([], `codex-read-${"a".repeat(64)}`), false,
  "a read revision or manual unread mark is not a terminal");
assert.equal(completionNeedsHistoryRepair([], `codex-read-${"a".repeat(64)}:native-turn`), true);
assert.equal(completionNeedsHistoryRepair([{
  id: "row", forkPointId: "native-turn", done: true, doneTs: 123,
}], `codex-read-${"a".repeat(64)}:native-turn`), false,
"native unread results retain exact history-repair identity");
assert.equal(completionNeedsHistoryRepair([{
  id: "browser-turn", forkPointId: "native-terminal",
  done: false, doneTs: undefined,
}], "native-terminal"), true,
"an exact but still-open cached row has not installed the terminal");
assert.equal(completionNeedsHistoryRepair([{
  id: "browser-turn", forkPointId: "native-terminal",
  done: true, doneTs: undefined,
}], "native-terminal"), true,
"done without the authoritative completion time still needs repair");
assert.equal(completionNeedsHistoryRepair([{
  id: "browser-turn", forkPointId: "native-terminal",
  done: true, doneTs: 12_345,
}], "native-terminal"), false,
"the exact completed native row suppresses redundant History reads");
assert.equal(completionNeedsHistoryRepair([{
  id: "different-native-turn", done: true, doneTs: 12_345,
}], "native-terminal"), true,
"an arbitrary completed last row cannot consume another completion receipt");

assert.equal(completionNeedsHistoryRepair([{
  id: "steer-before", forkPointId: "shared-native-task",
  done: true, doneTs: 12_000,
}, {
  id: "steer-after", forkPointId: "shared-native-task",
  liveTaskId: "shared-native-task", done: false,
}], "shared-native-task"), true,
"an older completed Codex steer segment cannot consume the latest terminal");
assert.equal(completionNeedsHistoryRepair([{
  id: "claude-user", checkpointId: "claude-user",
  forkPointId: "background-answer", done: true, doneTs: 12_345,
  blocks: [{
    kind: "text", message_id: "main-answer",
    text: "started in background", done: true, channel: "final",
  }],
}], "main-answer"), false,
"a persisted Claude assistant-id receipt converges after a background follow-up");
assert.equal(completionNeedsHistoryRepair([{
  id: "stale-process", checkpointId: "stale-process",
  done: true, doneTs: 12_345,
  blocks: [{
    kind: "process", item_id: "foreground-command",
    processKind: "command", phase: "start", status: "running",
    title: "build", done: false,
  }],
}], "stale-process"), true,
"a done row whose foreground process still animates has no visible footer");
assert.equal(completionNeedsHistoryRepair([{
  id: "background-process", checkpointId: "background-process",
  done: true, doneTs: 12_345,
  blocks: [{
    kind: "process", item_id: "detached-agent",
    processKind: "agent", phase: "start", status: "running",
    title: "agent", done: false, background: true,
  }],
}], "background-process"), false,
"genuine detached work does not suppress the enclosing completion footer");

assert.equal(idleTurnNeedsHistoryRepair([{
  id: "failed-open", done: false,
}]), true,
"an idle failed/interrupted tail can recover without a success receipt");
assert.equal(idleTurnNeedsHistoryRepair([{
  id: "settled-tail", done: true, doneTs: 12_345,
}]), false,
"a fully settled idle tail does not create an extra History read");

const firstTerminalRepair = nextTerminalHistoryRepairAttempt(
  undefined, "completion-1", 2);
assert.deepEqual(firstTerminalRepair, {
  key: "completion-1", attempts: 1, pending: true,
});
assert.equal(nextTerminalHistoryRepairAttempt(
  firstTerminalRepair ?? undefined, "completion-1", 2), null,
"an accepted repair remains single-flight until a History response arrives");
const settledTerminalRepair = settleTerminalHistoryRepairAttempt(
  firstTerminalRepair ?? undefined);
assert.deepEqual(nextTerminalHistoryRepairAttempt(
  settledTerminalRepair ?? undefined, "completion-1", 2), {
  key: "completion-1", attempts: 2, pending: true,
}, "an authoritative response releases exactly one bounded retry");
assert.equal(nextTerminalHistoryRepairAttempt({
  key: "completion-1", attempts: 2, pending: false,
}, "completion-1", 2), null,
"a source which cannot converge cannot create an unbounded History loop");
assert.deepEqual(nextTerminalHistoryRepairAttempt({
  key: "completion-1", attempts: 2, pending: false,
}, "completion-2", 2), {
  key: "completion-2", attempts: 1, pending: true,
}, "a genuinely newer terminal gets its own recovery budget");
assert.equal(completionNeedsHistoryRepair([], null), false,
  "a cleared receipt never manufactures a terminal recovery");

const appSource = readFileSync(resolve(process.cwd(), "src/App.tsx"), "utf8");
assert.match(appSource,
  /completionNeedsHistoryRepair\([\s\S]{0,120}historyView\.turns, completionId\)[\s\S]{0,1400}supersedePending: true/,
  "a missing durable completion supersedes an older History read");
assert.match(appSource,
  /const terminalRepairState = mergeSessionActivityState\(/,
  "terminal repair treats either runtime or catalog activity as running");
assert.match(appSource, /terminalRepairState !== "idle"/,
  "terminal repair waits for the merged idle boundary");
assert.match(appSource,
  /settledHistoryCausalKey\s*=\s*completedHistory\.settledCausalKey/,
  "the History coordinator exposes the exact completed causal request");
assert.match(appSource,
  /msg\.type === "history" && !msg\.before[\s\S]{0,220}settledHistoryCausalKey[\s\S]{0,180}settleTerminalHistoryRepair/,
  "only the causally matched authoritative head response releases a retry");
assert.match(appSource,
  /msg\.checkpoint_id \?\? msg\.turn_id \?\? null/,
  "the browser fallback receipt uses Claude's stable checkpoint identity");
