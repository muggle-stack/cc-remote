import type { ResponseProcessSummary } from "./protocol";
import type { Block, ProcessBlock, TextBlock, Turn } from "./domain/conversation";
import { finalTextBlocks } from "./process-blocks";
import { isReturnReceipt, isSyntheticReturnReceipt, returnedAgents, hasParentContinuation, returnAnchorIds } from "./agent-returns.ts";

export interface AssistantContinuation {
  id: string;
  /** Presentation identity survives native UUID binding and summary decoration. */
  renderKey: string;
  blocks: Block[];
  answers: TextBlock[];
  returns: ProcessBlock[];
  startedTs?: number;
  doneTs?: number;
  processSummary?: ResponseProcessSummary;
}

/** Attribute the enclosing terminal to one response, not every continuation.
 * Earlier provider errors remain visible in the original answer text. */
export function continuationTerminalOwner(
  turn: Turn, continuations: AssistantContinuation[],
): AssistantContinuation | undefined {
  if (!turn.done) return undefined;
  const exact = turn.forkPointId && continuations.find((segment) => segment.blocks.some((block) =>
    block.kind === "text" && (block.message_id === turn.forkPointId
      || block.stream_message_id === turn.forkPointId)));
  if (exact) return exact;
  // Live stream aliases can differ from the assembled terminal UUID. Native
  // source clocks still fence an error before a later continuation starts.
  for (let index = continuations.length - 1; index >= 0; index -= 1) {
    const segment = continuations[index];
    if (segment.startedTs != null && turn.doneTs != null
        && segment.startedTs <= turn.doneTs) return segment;
  }
  return undefined;
}

/** Native receipt order for Codex, background main-agent output for Claude.
 * Source order and native message IDs keep live, replay and detail identical. */
export function assistantContinuations(blocks: Block[], answers: TextBlock[], engine: "claude" | "codex" | "dsh" = "claude",
  responseProcesses?: ResponseProcessSummary[] | null) {
  const original: Block[] = [];
  const continuations: AssistantContinuation[] = [];
  const codex = engine === "codex";
  const visibleAnswers = new Map(answers.map((block) => [block.message_id, block]));
  let current: AssistantContinuation | undefined;
  let answered = false;
  for (const block of blocks) {
    if (isSyntheticReturnReceipt(block)) continue;
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
        renderKey: block.kind === "text" ? `text:${block.stream_message_id ?? block.message_id}`
          : block.kind === "tool" ? `tool:${block.tool_use_id}` : `process:${block.item_id}`,
        blocks: [], answers: [], returns: [], startedTs: block.startedTs,
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
  // Decorate existing native continuations only. Receipt arrival cannot change
  // their IDs, ordering, clocks, process blocks or disclosure state. In-flight
  // foreground output consumes the receipt without adding a visible boundary.
  const firstParentBlocks = new Map(continuations.flatMap(segment => {
    const first = segment.blocks.find(block => hasParentContinuation([block]));
    return first ? [[first, segment] as const] : [];
  }));
  let pending: Block[] = [];
  for (const block of blocks) {
    if (isReturnReceipt(block) && !(block.kind === "process" && block.returnAnchorId)) pending.push(block);
    if (hasParentContinuation([block])) {
      const segment = firstParentBlocks.get(block);
      if (segment) segment.returns = returnedAgents(pending);
      pending = [];
    }
  }
  const anchoredSegments = new Map(continuations.flatMap(segment => segment.blocks.flatMap(
    block => returnAnchorIds(block).map(id => [id, segment] as const))));
  for (const receipt of blocks) {
    if (!isReturnReceipt(receipt) || receipt.kind !== "process" || !receipt.returnAnchorId) continue;
    const segment = anchoredSegments.get(receipt.returnAnchorId);
    if (segment) segment.returns = returnedAgents([...segment.returns, receipt]);
  }
  if (engine === "claude" && responseProcesses) {
    for (const segment of continuations) {
      const summary = responseProcesses.find(row => row.background && (
        row.id === segment.id || segment.answers.some(answer => answer.message_id === row.answerId)));
      if (!summary) continue;
      segment.processSummary = summary;
      segment.id = summary.id;
      segment.startedTs = summary.startedTs ?? segment.startedTs;
      segment.doneTs = summary.doneTs ?? segment.doneTs;
    }
  }
  return {
    original,
    answers: [...visibleAnswers.values()],
    continuations,
  };
}
