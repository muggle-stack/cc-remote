import type { Block, TextBlock } from "./domain/conversation";
import { finalTextBlocks } from "./process-blocks";

export interface ClaudeContinuation {
  id: string;
  blocks: Block[];
  answers: TextBlock[];
  startedTs?: number;
  doneTs?: number;
}

/** Native task notifications update an existing child in place. Only actual
 * main-agent output starts a new narrative segment after the settled answer.
 * Source order and native message IDs keep live, replay and detail identical. */
export function claudeContinuations(blocks: Block[], answers: TextBlock[]) {
  const original: Block[] = [];
  const continuations: ClaudeContinuation[] = [];
  const continuationAnswers = new Set<string>();
  const visibleAnswers = new Map(answers.map((block) => [block.message_id, block]));
  let current: ClaudeContinuation | undefined;
  let answered = false;
  for (const block of blocks) {
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
    if (!current || answered) {
      current = {
        id: block.kind === "text" ? block.message_id
          : block.kind === "tool" ? block.tool_use_id : block.item_id,
        blocks: [], answers: [], startedTs: block.startedTs,
      };
      continuations.push(current);
      answered = false;
    }
    current.blocks.push(block);
    const doneTs = block.kind === "process" ? block.terminalTs : block.doneTs;
    if (doneTs != null) current.doneTs = Math.max(current.doneTs ?? 0, doneTs);
    if (block.kind === "text" && finalTextBlocks([block]).length > 0) {
      answered = true;
      const answer = visibleAnswers.get(block.message_id);
      if (answer) {
        current.answers.push(answer);
        continuationAnswers.add(block.message_id);
      }
    }
  }
  return {
    original,
    answers: answers.filter((block) => !continuationAnswers.has(block.message_id)),
    continuations,
  };
}
