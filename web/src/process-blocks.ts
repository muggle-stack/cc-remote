import type { Block, TextBlock } from "./reducer";

export function processBlocks(blocks: Block[]): Block[] {
  return blocks.filter((block) => block.kind !== "text"
    || (block.text.length > 0
      && (block.channel === "thinking" || block.channel === "commentary")));
}

export function finalTextBlocks(blocks: Block[]): TextBlock[] {
  return blocks.filter((block): block is TextBlock => block.kind === "text"
    && block.text.length > 0
    && (block.channel == null || block.channel === "final" || block.channel === "unknown"));
}

/** A main answer can finish before a background task or agent reports its
 * final lifecycle event. Keep the process shell live for those late updates
 * instead of presenting a running child as an already-completed turn. */
export function hasActiveProcess(blocks: Block[]): boolean {
  return blocks.some((block) =>
    (block.kind === "tool" || block.kind === "process") && !block.done);
}
