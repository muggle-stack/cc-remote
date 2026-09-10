import type { Block, ProcessBlock, TextBlock, Turn } from "./domain/conversation";

export function generatedImageIdentity(block: ProcessBlock): string {
  const ref = block.input?.history_image as { image_id?: unknown } | undefined;
  return typeof ref?.image_id === "string" ? ref.image_id : block.item_id;
}

export function modelFallbackNotices(blocks: readonly Block[]): ProcessBlock[] {
  return [...new Map(blocks.flatMap((block) => (
    block.kind === "process" && block.processKind === "model"
      && block.tool === "model_refusal_fallback"
      ? [[block.item_id, block] as const] : []
  ))).values()];
}

/** Show distinct output images, not duplicate live/history views of the same
 * content. Every native activity stays in the timeline; only the gallery is
 * deduplicated. Prefer its live snapshot handle while available. */
export function generatedOutputImages(blocks: readonly Block[]): ProcessBlock[] {
  const images = new Map<string, ProcessBlock>();
  for (const block of blocks) {
    if (block.kind !== "process" || block.tool !== "image_generation"
        || block.status !== "succeeded" || block.phase !== "end" || !block.done) continue;
    const identity = generatedImageIdentity(block);
    const previous = images.get(identity);
    if (!previous || typeof block.input?.preview_id === "string") images.set(identity, block);
  }
  return [...images.values()].slice(-8);
}

/** Resolve runtime activity onto exactly one displayed narrative row. Session
 * state alone is deliberately insufficient: aliases can collide in migrated
 * caches, and a stale owner must not animate an unrelated historical turn. */
export function exactActiveTurnId(
  turns: readonly Pick<Turn, "id" | "clientMsgId" | "historyTurnId">[],
  ownerTurnId: string | null | undefined,
  active: boolean,
): string | null {
  const candidates = activeTurnCandidateIds(turns, ownerTurnId, active);
  return candidates.length === 1 ? candidates[0] : null;
}

/** Return only rows which exactly alias the active native/browser owner. An
 * empty result means inactive, absent from this projection, or unowned;
 * multiple results are the one ambiguity ChatView may resolve to a latest row. */
export function activeTurnCandidateIds(
  turns: readonly Pick<Turn, "id" | "clientMsgId" | "historyTurnId">[],
  ownerTurnId: string | null | undefined,
  active: boolean,
): string[] {
  if (!active || !ownerTurnId) return [];
  return turns.flatMap((turn) => [
    turn.id, turn.clientMsgId, turn.historyTurnId,
  ].includes(ownerTurnId) ? [turn.id] : []);
}

/** A newly submitted browser turn is an explicit, already-painted owner. It
 * must win over the prior native owner retained for late-event correlation;
 * otherwise the working spark briefly jumps back to the completed row until
 * the engine acknowledges and binds the new turn. */
export function displayActiveTurnOwnerId(
  liveOwnerTurnId: string | null | undefined,
  acceptancePending: string | null | undefined,
): string | null {
  return acceptancePending ?? liveOwnerTurnId ?? null;
}

export function processBlocks(blocks: Block[]): Block[] {
  const dedicatedAgents = new Set(blocks.flatMap((block) => (
    block.kind === "process" && block.processKind === "agent" && block.parent_id
      ? [block.parent_id] : []
  )));
  return blocks.filter((block) => {
    if (block.kind === "process" && block.tool === "model_refusal_fallback") return false;
    if (block.kind === "text") {
      return block.text.length > 0
        && block.delivery !== "async"
        && (block.channel === "thinking" || block.channel === "commentary");
    }
    // Keep ToolUse in reducer state for result correlation and older peers,
    // while presenting the dedicated live agent lifecycle only once.
    if (block.kind === "tool"
        && (block.category === "agent"
          || ["agent", "task"].includes(block.tool.toLowerCase()))) {
      return !dedicatedAgents.has(block.tool_use_id);
    }
    return true;
  });
}

export function isCodexPresentationNoise(block: Block): boolean {
  if (block.kind === "text" && block.channel === "thinking") return true;
  if (block.kind !== "process") return false;
  if (block.processKind === "reasoning") return true;
  if (block.processKind !== "hook") return false;
  // Successful/pending hooks are plumbing around useful tool activity. Keep
  // only actionable hook failures in Codex's public process projection.
  return !["failed", "declined", "cancelled", "interrupted"].includes(
    block.status,
  );
}

export function presentableProcessBlocks(
  blocks: Block[],
  engine: "claude" | "codex" | "dsh",
): Block[] {
  const items = processBlocks(blocks);
  return engine === "codex"
    ? items.filter((block) => !isCodexPresentationNoise(block))
    : items;
}

export function finalTextBlocks(blocks: Block[]): TextBlock[] {
  return blocks.filter((block): block is TextBlock => block.kind === "text"
    && block.text.length > 0
    && (block.delivery === "async"
      || block.channel == null || block.channel === "final" || block.channel === "unknown"));
}

/** A main answer can finish before a background task or agent reports its
 * final lifecycle event. Keep the process shell live for those late updates
 * instead of presenting a running child as an already-completed turn. */
export function hasActiveProcess(blocks: Block[]): boolean {
  return blocks.some((block) =>
    (block.kind === "tool" || block.kind === "process") && !block.done);
}
