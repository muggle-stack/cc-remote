import assert from "node:assert/strict";
import { mergeDetailWithLiveTail, mergeInitialHistory } from "../src/history-merge.ts";
import type { Block, TextBlock, Turn } from "../src/domain/conversation.ts";

const opening: TextBlock = {
  kind: "text", message_id: "opening", channel: "commentary", done: true,
  text: "已说明弹窗原因。\n\n已迁移验收环境。",
};
const base: Turn = { id: "long-task", prompt: "run acceptance", done: false, blocks: [] };
let live: Turn = { ...base, blocks: [], liveBlocksSpilled: true, liveSpillBlocks: [opening] };
const observed: Block[] = [opening];

// A long Codex task keeps its opening and newest commentary in the lightweight
// summary, while earlier items are in spill/detail. Each new tool invocation
// opens an empty commentary envelope. Refreshing history during that window
// used to copy the opening into the empty envelope, once per refresh cycle.
for (let cycle = 0; cycle < 12; cycle += 1) {
  const recent: TextBlock = {
    kind: "text", message_id: `progress-${cycle}`, channel: "commentary",
    text: `进展 ${cycle} 已完成。`, done: true,
  };
  const toolId = `exec-${cycle}`;
  const envelope: TextBlock = {
    kind: "text", message_id: toolId, channel: "commentary", text: "", done: false,
  };
  const tool: Block = {
    kind: "tool", tool_use_id: toolId, message_id: toolId,
    tool: "exec", input: {}, done: false,
  };
  const summary = { ...base, blocks: [opening, recent] };
  live = { ...live, liveSpillBlocks: [...observed], blocks: [recent, envelope, tool] };
  for (let refresh = 0; refresh < 3; refresh += 1) {
    [live] = mergeInitialHistory([summary], [live], { preserveLiveTailOpen: true });
    const timeline = mergeDetailWithLiveTail(live.liveSpillBlocks ?? [], live.blocks);
    assert.equal(timeline.filter(block => block.kind === "text"
      && block.text === opening.text).length, 1,
    "a running task paints its opening once across repeated history refreshes");
    assert.equal((live.blocks.find(block => block.kind === "text"
      && block.message_id === toolId) as TextBlock).text, "",
    "a tool's empty envelope never inherits narrative text");
  }
  observed.push(recent, { ...envelope, done: true }, { ...tool, done: true });
}

// Empty real assistant messages also have no cross-ID overlap evidence. The
// first delta must belong only to that new message. An exact ID still hydrates.
for (const sameId of [false, true]) {
  const placeholder: TextBlock = { ...opening, message_id: sameId ? opening.message_id : "next-answer", text: "", done: false };
  const [merged] = mergeInitialHistory([{ ...base, blocks: [opening] }],
    [{ ...base, blocks: [placeholder] }], { preserveLiveTailOpen: true });
  const next = merged.blocks.find(block => block.kind === "text"
    && block.message_id === placeholder.message_id) as TextBlock;
  assert.equal(next.text, sameId ? opening.text : "");
}

const [settled] = mergeInitialHistory([{ ...base, done: true, blocks: observed }],
  [{ ...live, done: true }], {}, true);
const finalTimeline = mergeDetailWithLiveTail(settled.liveSpillBlocks ?? [], settled.blocks, true);
assert.equal(finalTimeline.filter(block => block.kind === "text"
  && block.text === opening.text).length, 1, "settling the task preserves the same single opening");

console.log("Codex empty tool-envelope history regressions passed");
