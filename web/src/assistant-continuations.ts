import type { Block, TextBlock } from "./domain/conversation";
import { finalTextBlocks } from "./process-blocks";

export interface AssistantContinuation {
  id: string;
  blocks: Block[];
  answers: TextBlock[];
  startedTs?: number;
  doneTs?: number;
}

/** Native receipt order for Codex, background main-agent output for Claude.
 * Source order and native message IDs keep live, replay and detail identical. */
export function assistantContinuations(blocks: Block[], answers: TextBlock[], engine: "claude" | "codex" = "claude") {
  const original: Block[] = [];
  const continuations: AssistantContinuation[] = [];
  const codex = engine === "codex";
  const visibleAnswers = new Map(answers.map((block) => [block.message_id, block]));
  let current: AssistantContinuation | undefined;
  let answered = false;
  for (const block of blocks) {
    let begins: boolean;
    if (codex) {
      const receipt = block.kind === "process" && block.processKind === "task"
        && block.server === "cc_remote_tasks" && block.tool === "task_result"
        && block.phase === "end";
      // Consecutive receipts consumed together share one source label.
      begins = receipt && (!current || answered);
      if (!current && !receipt) {
        original.push(block);
        continue;
      }
      answered = !receipt;
    } else {
      const child = block.kind === "process"
        && (block.processKind === "agent" || block.processKind === "task");
      const bookkeeping = block.kind === "process" && ![
        "command", "file_change", "mcp", "web_search", "server_tool", "reasoning",
      ].includes(block.processKind);
      if (block.background !== true || child
          || (bookkeeping && (!current || answered))
          || (block.kind === "text" && block.delivery === "async")) {
        original.push(block);
        continue;
      }
      begins = !current || answered;
      if (begins) answered = false;
    }
    if (begins || !current) {
      current = {
        id: block.kind === "text" ? block.message_id
          : block.kind === "tool" ? block.tool_use_id : block.item_id,
        blocks: [], answers: [], startedTs: block.startedTs,
      };
      continuations.push(current);
    }
    current.blocks.push(block);
    const doneTs = block.kind === "process" ? block.terminalTs : block.doneTs;
    if (doneTs != null) current.doneTs = Math.max(current.doneTs ?? 0, doneTs);
    if (block.kind === "text" && (codex || finalTextBlocks([block]).length > 0)) {
      if (!codex) answered = true;
      const answer = visibleAnswers.get(block.message_id);
      if (answer) {
        current.answers.push(answer);
        visibleAnswers.delete(block.message_id);
      }
    }
  }
  return {
    original,
    answers: [...visibleAnswers.values()],
    continuations,
  };
}
