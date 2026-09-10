import {
  UNKNOWN_TERMINAL_ERROR,
  type Block,
  type ProcessBlock,
  type TextBlock,
  type ToolBlock,
  type Turn,
  type TurnDetailProjection,
} from "./domain/conversation.ts";
import { reconcileProvenCompactionOrphans } from "./compaction-orphans.ts";
import { generatedImageIdentity, generatedOutputImages } from "./process-blocks.ts";
import { codexTransportInterruption } from "./problem-presentation.ts";
export { reconcileProvenCompactionOrphans } from "./compaction-orphans.ts";

function combineText(first: string, second: string): string {
  if (!first) return second;
  if (!second || first.includes(second)) return first;
  if (second.includes(first)) return second;
  const max = Math.min(first.length, second.length);
  for (let overlap = max; overlap > 0; overlap--) {
    if (first.slice(-overlap) === second.slice(0, overlap)) {
      return first + second.slice(overlap);
    }
  }
  return first + second;
}

function textChannel(block: TextBlock): string {
  return block.channel ?? "final";
}

function canCompatibilityMatchText(block: TextBlock): boolean {
  return block.text.length > 0 || !block.done;
}

function textAffinity(first: string, second: string): number {
  if (first === second) return Number.MAX_SAFE_INTEGER;
  if (first.includes(second) || second.includes(first)) {
    return Math.min(first.length, second.length);
  }
  const max = Math.min(first.length, second.length);
  for (let overlap = max; overlap > 0; overlap -= 1) {
    if (first.slice(-overlap) === second.slice(0, overlap)
        || second.slice(-overlap) === first.slice(0, overlap)) return overlap;
  }
  return 0;
}

function isFinalTextBlock(
  block: Block,
): block is TextBlock & { channel: "final" } {
  return block.kind === "text" && block.channel === "final";
}

type ProcessDetailState = NonNullable<Turn["processDetailState"]>;
type TurnDetailReason = NonNullable<Turn["detailReasons"]>[number];

function isPresentableProcessBlock(block: Block): boolean {
  if (block.kind === "tool") return true;
  if (block.kind === "text") {
    return block.channel === "commentary" && block.text.length > 0;
  }
  if (block.processKind === "reasoning") return false;
  if (block.processKind !== "hook") return true;
  return ["failed", "declined", "cancelled", "interrupted"].includes(
    block.status,
  );
}

function statedProcessDetailState(turn: Turn): ProcessDetailState {
  if (turn.processDetailState) return turn.processDetailState;
  return (turn.detailEventCount ?? 0) > 0 ? "unknown" : "none";
}

function hasDeferredTurnDetail(turn: Turn): boolean {
  return statedProcessDetailState(turn) !== "none"
    || (turn.detailReasons?.length ?? 0) > 0
    || (turn.detailEventCount ?? 0) > 0;
}

/** Merge only same-revision process evidence. A concrete browser/detail
 * projection refines an opaque native summary, while an opaque refresh can
 * never erase an exact conclusion already learned for that revision. */
function mergedProcessDetailState(
  history: Turn,
  live: Turn,
  blocks: readonly Block[],
): ProcessDetailState {
  if (blocks.some(isPresentableProcessBlock)) return "present";
  const historyState = statedProcessDetailState(history);
  const liveState = statedProcessDetailState(live);
  // Visible process is immutable within one history revision. A later opaque
  // or final-only page may replace the currently retained block window, but it
  // cannot prove that a process observed on another page never existed.
  if (historyState === "present" || liveState === "present") return "present";
  if (historyState !== "unknown") return historyState;
  if (liveState !== "unknown") return liveState;
  return "unknown";
}

function mergedDetailReasons(
  history: Turn,
  live: Turn,
  processState: ProcessDetailState,
): TurnDetailReason[] {
  const reasons = new Set<TurnDetailReason>([
    ...(history.detailReasons ?? []),
    ...(live.detailReasons ?? []),
  ]);
  if (processState === "present") reasons.add("process");
  else if (processState === "none") reasons.delete("process");
  return [...reasons];
}

function earliestTimestamp(
  first: number | undefined,
  second: number | undefined,
): number | undefined {
  if (first == null) return second;
  if (second == null) return first;
  return Math.min(first, second);
}

function latestTimestamp(
  first: number | undefined,
  second: number | undefined,
): number | undefined {
  if (first == null) return second;
  if (second == null) return first;
  return Math.max(first, second);
}

function trustworthyProcessStartedTs(turn: Turn): number | undefined {
  const started = turn.processStartedTs;
  const done = turn.processDoneTs;
  if (started == null) return undefined;
  // Older projections assigned every hydrated native item the same parser
  // timestamp. Do not let that invalid zero interval combine with a newer
  // source witness and manufacture a duration stretching to refresh time.
  if (done != null && done - started < 500) return undefined;
  return started;
}

function trustworthyProcessDoneTs(turn: Turn): number | undefined {
  const started = turn.processStartedTs;
  const done = turn.processDoneTs;
  if (started == null || done == null || done - started < 500) {
    return undefined;
  }
  return done;
}

function processBlockMatches(
  history: Block[],
  live: Block[],
): Map<number, number> {
  const matches = new Map<number, number>();
  const usedHistory = new Set<number>();
  const historyProcessIndexes = history.flatMap((block, index) =>
    block.kind === "process" ? [index] : []);
  const liveProcessIndexes = live.flatMap((block, index) =>
    block.kind === "process" ? [index] : []);

  // Native item ids are authoritative regardless of process kind.
  for (const liveIndex of liveProcessIndexes) {
    const liveBlock = live[liveIndex] as ProcessBlock;
    const historyIndex = historyProcessIndexes.find((index) =>
      !usedHistory.has(index)
      && (history[index] as ProcessBlock).item_id === liveBlock.item_id);
    if (historyIndex == null) continue;
    matches.set(liveIndex, historyIndex);
    usedHistory.add(historyIndex);
  }

  // Rollout and live app-server projections may assign different item ids to
  // the same contextCompaction occurrence. Pair only that authoritative process
  // kind, and pair by occurrence instead of collapsing an entire native task:
  // one long task can compact more than once.
  const historyCompactions = new Map<string, number[]>();
  const liveCompactions = new Map<string, number[]>();
  for (const historyIndex of historyProcessIndexes) {
    if (usedHistory.has(historyIndex)) continue;
    const block = history[historyIndex] as ProcessBlock;
    if (block.processKind !== "compaction" || !block.turn_id) continue;
    const indexes = historyCompactions.get(block.turn_id) ?? [];
    indexes.push(historyIndex);
    historyCompactions.set(block.turn_id, indexes);
  }
  for (const liveIndex of liveProcessIndexes) {
    if (matches.has(liveIndex)) continue;
    const block = live[liveIndex] as ProcessBlock;
    if (block.processKind !== "compaction" || !block.turn_id) continue;
    const indexes = liveCompactions.get(block.turn_id) ?? [];
    indexes.push(liveIndex);
    liveCompactions.set(block.turn_id, indexes);
  }
  for (const [turnId, liveIndexes] of liveCompactions) {
    const historyIndexes = historyCompactions.get(turnId);
    if (!historyIndexes?.length) continue;
    const pairCount = Math.min(historyIndexes.length, liveIndexes.length);
    // Live is normally the current tail of a longer transcript projection.
    // When History has more occurrences, align that live tail to History's
    // tail. If live has more, its unmatched suffix is genuinely newer.
    const historyStart = historyIndexes.length - pairCount;
    for (let offset = 0; offset < pairCount; offset += 1) {
      const liveIndex = liveIndexes[offset];
      const historyIndex = historyIndexes[historyStart + offset];
      matches.set(liveIndex, historyIndex);
      usedHistory.add(historyIndex);
    }
  }
  return matches;
}

function mergeBlocks(
  history: Block[],
  live: Block[],
  preserveLiveOpen: boolean,
  preferCompletedHistoryPayload = false,
  allowCompatibilityTextMatch = true,
  completedTextAuthority: "first" | "second" | "combine" = "first",
  settledCanonicalText = false,
): Block[] {
  const out = history.map((block) => ({ ...block }));
  const processMatches = processBlockMatches(history, live);
  // Native message identity is the only safe text overlap proof. Two separate
  // commentary items may intentionally contain identical text; content-based
  // matching silently deletes the later item and corrupts cache/detail/spill.
  const historyTextIndexes = out.flatMap((block, index) =>
    block.kind === "text" ? [index] : []);
  const matchedTextIndexes = new Set<number>();
  const exactTextMatches = new Map<number, number>();
  const reservedExactHistoryIndexes = new Set<number>();
  for (let liveIndex = live.length - 1; liveIndex >= 0; liveIndex -= 1) {
    const block = live[liveIndex];
    if (block.kind !== "text") continue;
    const historyIndex = historyTextIndexes.find((index) =>
      !reservedExactHistoryIndexes.has(index)
      && (out[index] as TextBlock).message_id === block.message_id);
    if (historyIndex == null) continue;
    exactTextMatches.set(liveIndex, historyIndex);
    reservedExactHistoryIndexes.add(historyIndex);
  }
  for (let liveIndex = 0; liveIndex < live.length; liveIndex += 1) {
    const block = live[liveIndex];
    if (block.kind === "process") {
      const historyIndex = processMatches.get(liveIndex);
      const existing = historyIndex == null
        ? undefined : out[historyIndex] as ProcessBlock;
      if (existing) {
        const historyLifecycle = {
          done: existing.done, phase: existing.phase, status: existing.status,
          title: existing.title, progress: existing.progress,
        };
        const keepHistoryPayload = preferCompletedHistoryPayload
          && existing.done && block.done;
        const plan = keepHistoryPayload
          ? existing.plan : block.plan ?? existing.plan;
        if (!keepHistoryPayload) Object.assign(existing, block);
        if (plan) existing.plan = plan.map((entry) => ({ ...entry }));
        // A completed transcript is authoritative over stale cache/live state.
        // Only the explicit in-flight-tail merge may reopen a synthetic history
        // boundary while the same live turn is genuinely still running.
        if (!preserveLiveOpen && historyLifecycle.done && !block.done) {
          Object.assign(existing, historyLifecycle);
        }
      } else {
        out.push({ ...block, plan: block.plan?.map((entry) => ({ ...entry })) });
      }
      continue;
    }
    if (block.kind === "tool") {
      const existing = out.find((candidate) => candidate.kind === "tool"
        && candidate.tool_use_id === block.tool_use_id) as ToolBlock | undefined;
      if (existing) {
        const historyDone = existing.done;
        const historyResult = existing.result;
        const historyTitle = existing.title;
        const historyProgress = existing.progress;
        const keepHistoryPayload = preferCompletedHistoryPayload
          && existing.done && block.done;
        if (!keepHistoryPayload) Object.assign(existing, block);
        if (!preserveLiveOpen && historyDone && !block.done) {
          existing.done = true;
          if (historyResult) existing.result = historyResult;
          existing.title = historyTitle;
          existing.progress = historyProgress;
        }
      }
      else out.push({ ...block });
      continue;
    }
    let existingIndex = exactTextMatches.get(liveIndex);
    // Only complete history/live turn reconciliation owns this compatibility
    // fallback: those two projections already have an authoritative turn alias.
    // Detail/spill/cache windows do not carry overlap provenance, so equal text
    // there is never proof that two native items are the same occurrence.
    if (allowCompatibilityTextMatch && existingIndex == null
        && canCompatibilityMatchText(block)) {
      const candidates = historyTextIndexes.filter((index) => {
        const candidate = out[index] as TextBlock;
        return !matchedTextIndexes.has(index)
          && !reservedExactHistoryIndexes.has(index)
          && textChannel(candidate) === textChannel(block)
          && candidate.delivery === block.delivery
          && canCompatibilityMatchText(candidate);
      });
      let bestScore = 0;
      for (const index of candidates) {
        const score = textAffinity((out[index] as TextBlock).text, block.text);
        if (score > bestScore) {
          bestScore = score;
          existingIndex = index;
        }
      }
      if (existingIndex == null && block.text.length === 0 && !block.done
          && candidates.length === 1) existingIndex = candidates[0];
    }
    const existing = existingIndex == null
      ? undefined : out[existingIndex] as TextBlock;
    if (existing) {
      matchedTextIndexes.add(existingIndex!);
      const sameNativeMessage = existing.message_id === block.message_id;
      // A completed native item is immutable. For an exact stable message id,
      // the caller selects the source-backed payload: initial History keeps its
      // bounded canonical text, while an already-loaded detail page keeps its
      // complete text across a later summary refresh. This deliberately uses
      // identity and lifecycle only: content heuristics cannot distinguish a
      // replay duplicate from legitimate repeated prose or a truncated summary.
      existing.text = sameNativeMessage && existing.done && block.done
        ? completedTextAuthority === "first"
          ? existing.text
          : completedTextAuthority === "second"
            ? block.text
            : combineText(existing.text, block.text)
        : combineText(existing.text, block.text);
      existing.done = existing.done || block.done;
      if (block.channel !== "unknown") existing.channel = block.channel;
      if (block.delivery === "async") {
        existing.delivery = "async";
        if (block.questions?.length) existing.questions = block.questions;
      }
      if (block.liveOrder != null) {
        existing.liveOrder = existing.liveOrder == null
          ? block.liveOrder : Math.min(existing.liveOrder, block.liveOrder);
      }
      // History parsers can regenerate an assistant item id. While this turn
      // is still open, future deltas continue targeting the live app-server id.
      // Keeping the history id here makes the next delta create a second block,
      // which then survives every focus-triggered History reconciliation.
      if (preserveLiveOpen) existing.message_id = block.message_id;
    } else if (settledCanonicalText && block.done) {
      // A settled Codex source page is authoritative for completed lightweight
      // text items. An older IndexedDB projection can carry a regenerated id,
      // while reconnect replay already carries the exact canonical id. Once
      // that exact sibling is reserved above, an identical completed local
      // block is the stale third projection, not a second native item. History
      // would contain both if both really existed, so this is deliberately
      // unavailable to running, paged, cache-only and Claude merges.
      const canonicalDuplicate = historyTextIndexes.some((index) => {
        if (!reservedExactHistoryIndexes.has(index)) return false;
        const candidate = out[index] as TextBlock;
        return candidate.done
          && textChannel(candidate) === textChannel(block)
          && candidate.delivery === block.delivery
          && candidate.text === block.text;
      });
      if (!canonicalDuplicate) out.push({ ...block });
    } else {
      out.push({ ...block });
    }
  }
  return out;
}

/** Combine a source-backed detail window with the bounded live tail.
 *
 * Detail pages can be fetched while a turn is still running. New stream frames
 * continue updating Turn.blocks, so rendering the projection alone would make
 * those later frames disappear until another history request. Merge by native
 * block identity and discard only the reducer's old presentation-only marker. */
export function mergeDetailWithLiveTail(
  detail: readonly Block[],
  live: readonly Block[],
  preferCompletedDetailPayload = false,
): Block[] {
  const withoutOldOmissionMarker = (block: Block) => !(
    block.kind === "process"
    && (block.item_id === "__cc_remote_earlier_process_omitted__"
      || block.item_id === "__cc_remote_detail_projection_capped__")
  );
  const filteredDetail = detail.filter(withoutOldOmissionMarker);
  const filteredLive = live.filter(withoutOldOmissionMarker);
  const merged = mergeBlocks(
    filteredDetail,
    filteredLive,
    true,
    preferCompletedDetailPayload,
    false,
    preferCompletedDetailPayload ? "first" : "combine",
  );

  // Spill archives and the bounded live row are two slices of one reducer
  // chronology. Compaction repair may rebase both slices together, so their
  // array concatenation is no longer chronological. If every merged block has
  // one finite unique order, use that complete proof for the final render.
  // Any source-only block without liveOrder deliberately keeps the existing
  // detail-first behavior below.
  const mergedOrders = merged.map((block) => block.liveOrder);
  if (merged.length > 0
      && mergedOrders.every(
        (order): order is number => Number.isFinite(order),
      )
      && new Set(mergedOrders).size === merged.length) {
    return [...merged].sort(
      (left, right) => left.liveOrder! - right.liveOrder!);
  }

  // Official Codex full/summary views can omit command/tool items while the
  // browser has already observed the complete interleaved live sequence. When
  // the source page is a subset of that live sequence, its array order is not a
  // chronology authority: using it first moves every missing tool behind the
  // commentary. Keep the source payload merge above, but paint in the complete
  // live order. A genuine source superset (normal paged detail + live tail)
  // retains its source order and simply appends the new tail as before.
  const liveIds = new Set(filteredLive.map(blockIdentity));
  const liveOrders = filteredLive.map((block) => block.liveOrder);
  if (filteredLive.length > 0
      && liveOrders.every((order): order is number => Number.isFinite(order))
      && new Set(liveOrders).size === filteredLive.length
      && liveIds.size === filteredLive.length
      && new Set(filteredDetail.map(blockIdentity)).size === filteredDetail.length
      && filteredDetail.every((block) => liveIds.has(blockIdentity(block)))) {
    const byId = new Map(merged.map((block) => [blockIdentity(block), block]));
    const ordered = [...filteredLive]
      .sort((left, right) => left.liveOrder! - right.liveOrder!)
      .flatMap((block) => {
        const key = blockIdentity(block);
        const resolved = byId.get(key);
        if (!resolved) return [];
        byId.delete(key);
        return [resolved];
      });
    return [...ordered, ...byId.values()];
  }
  return merged;
}

type TurnIdentity = Pick<
  Turn,
  "id" | "clientMsgId" | "historyTurnId" | "forkPointId"
>;

function exactTurnAliases(turn: TurnIdentity): Set<string> {
  return new Set([
    turn.id,
    turn.clientMsgId,
    turn.historyTurnId,
  ].filter((value): value is string => !!value));
}

function sharesExactTurnAlias(first: TurnIdentity, second: TurnIdentity): boolean {
  const firstAliases = exactTurnAliases(first);
  return [...exactTurnAliases(second)].some((alias) => firstAliases.has(alias));
}

function nativeTaskIdentity(turn: Turn): string | undefined {
  return turn.liveTaskId ?? turn.forkPointId ?? turn.codexTurnId;
}

function historicalInterruptionError(history: Turn, previous: Turn): string | undefined {
  if (history.error != null) return history.error;
  // Native history knows the outcome, but a daemon replacement's cause exists
  // only on the control link. Keep it only for the exact interrupted task.
  const nativeId = nativeTaskIdentity(history);
  return history.done && history.interrupted
    && nativeId && nativeId === nativeTaskIdentity(previous)
    && codexTransportInterruption(previous.error) ? previous.error : undefined;
}

/** Restore observed control-link causes within the caller's validated history
 * revision/generation, without resurrecting rows or changing native outcomes. */
export function restoreTurnInterruptionCauses(
  summaries: Turn[], previous: readonly Turn[],
): Turn[] {
  const observed = previous.filter(turn => codexTransportInterruption(turn.error));
  if (!observed.length) return summaries;
  return summaries.map(summary => {
    if (!summary.done || !summary.interrupted || summary.error != null) return summary;
    const matches = observed.filter(turn => sharesExactTurnAlias(summary, turn)
      && nativeTaskIdentity(summary) === nativeTaskIdentity(turn));
    if (matches.length !== 1) return summary;
    const error = historicalInterruptionError(summary, matches[0]);
    return error ? { ...summary, error } : summary;
  });
}

function isIdleHistoryRestartOrphan(turn: Turn): boolean {
  return turn.done
    && turn.interrupted === true
    && turn.error === UNKNOWN_TERMINAL_ERROR
    && (turn.terminalSource == null
      || turn.terminalSource === "idle_history_recovery")
    && !turn.prompt
    && !turn.clientMsgId
    && !turn.historyTurnId
    && !turn.checkpointId
    && !turn.images?.length
    && !turn.imageRefs?.length
    && !turn.files?.length
    && turn.blocks.length > 0
    && turn.blocks.every((block) => block.done);
}

function blockIdentity(block: Block): string {
  return block.kind === "text"
    ? `text:${block.message_id}`
    : block.kind === "tool"
      ? `tool:${block.tool_use_id}`
      : `process:${block.item_id}`;
}

function compactionTurnAliases(turn: Turn): Set<string> {
  return new Set(turn.blocks.flatMap((block) =>
    block.kind === "process"
      && block.processKind === "compaction"
      && block.turn_id
      ? [block.turn_id]
      : []));
}

function sharesCompactionTurnAlias(history: Turn, live: Turn): boolean {
  // Official Codex history keeps the visible user-message id as the row id and
  // exposes the enclosing native task as forkPointId. A live compaction marker
  // carries that native task id before the user-message binding can arrive.
  // This native alias is safe only for compaction: ordinary process rows from
  // multiple steered segments intentionally share one enclosing task id.
  // The prompt guard is equally important: compaction and two steered prompts
  // may all share that task id, but they are still distinct visible rows.
  if (history.prompt !== live.prompt) return false;
  const historyAliases = new Set([
    ...exactTurnAliases(history),
    ...compactionTurnAliases(history),
    history.forkPointId,
  ].filter((value): value is string => !!value));
  const liveAliases = new Set([
    ...exactTurnAliases(live),
    ...compactionTurnAliases(live),
    live.forkPointId,
  ].filter((value): value is string => !!value));
  return [...compactionTurnAliases(live)].some(
    (alias) => historyAliases.has(alias),
  ) || [...compactionTurnAliases(history)].some(
    (alias) => liveAliases.has(alias),
  );
}

function sameTurnIdentity(history: Turn, live: Turn): boolean {
  if (sharesExactTurnAlias(history, live)) return true;
  // Codex may emit contextCompaction before the clean user/message binding.
  // The process event then carries the only native identity available to the
  // optimistic row. Treat that one authoritative marker as a turn alias, but
  // never generalize ordinary process turn ids: multiple steered narrative
  // segments legitimately share one native task id.
  if (sharesCompactionTurnAlias(history, live)) return true;
  // Automatic/goal continuations have no user message. Live uses the app-server
  // turn id as its empty anchor, while rollout history may use the first
  // assistant item id; TurnEnd still supplies the same authoritative branch id.
  if (history.forkPointId && live.forkPointId
      && history.forkPointId === live.forkPointId) return true;
  return history.forkPointId === live.id || live.forkPointId === history.id;
}

function sameTurn(history: Turn, live: Turn): boolean {
  return sameTurnIdentity(history, live);
}

function sameLegacyCachedTurn(summary: Turn, cached: Turn): boolean {
  if (sameTurnIdentity(summary, cached)) return true;
  if (!summary.prompt || summary.prompt !== cached.prompt) return false;
  // CACHE_VER migrations may predate clientMsgId/historyTurnId. Keep this
  // compatibility only while restoring a one-to-one completed cache row;
  // ordinary history/live reconciliation must never infer identity from text.
  if (summary.ts == null || cached.ts == null) return false;
  return Math.abs(summary.ts - cached.ts) <= 3000;
}

export function historyContainsTurn(history: Turn[], live: Turn): boolean {
  return history.some((turn) => sameTurn(turn, live));
}

function cloneDetailBlock(block: Block): Block {
  if (block.kind === "process") {
    return {
      ...block,
      input: block.input ? { ...block.input } : block.input,
      plan: block.plan?.map((entry) => ({ ...entry })),
    };
  }
  if (block.kind === "tool") {
    return {
      ...block,
      input: { ...block.input },
      result: block.result ? { ...block.result } : block.result,
    };
  }
  return { ...block };
}

/** A completed authoritative summary is the terminal fence for provisional
 * detail restored from IndexedDB.  The cache may have been written between a
 * child start and its final update; replaying that open bit after refresh makes
 * an hour-old answer show a permanently animated "处理中" spark. */
export type CachedDetailAuthority = "provisional" | "running" | "idle";

function cloneSettledCachedDetailBlock(
  block: Block,
  summary: Turn,
  preserveOpenPlans: boolean,
): Block {
  const cloned = cloneDetailBlock(block);
  if (cloned.kind === "text") {
    cloned.done = true;
  } else if (cloned.kind === "process" && !cloned.done
      && !(preserveOpenPlans && cloned.processKind === "plan")) {
    cloned.done = true;
    if (!cloned.status || cloned.status === "running") {
      cloned.status = summary.interrupted
        ? "interrupted" : summary.error ? "failed" : "succeeded";
    }
  } else if (cloned.kind === "tool" && !cloned.done) {
    cloned.done = true;
    cloned.result ??= {
      content: cloned.output ?? "",
      is_error: !!summary.interrupted || !!summary.error,
      status: summary.interrupted
        ? "interrupted" : summary.error ? "failed" : "succeeded",
    };
  }
  return cloned;
}

/** Repair the one pre-v36 cache shape which treated every task correlation as
 * a clickable Agent. A real Agent process is parented by an Agent/Task tool;
 * an explicitly observed non-Agent parent is therefore safe to demote. When
 * the parent shell was evicted, retain the block unchanged and let canonical
 * TurnDetail replace it instead of guessing. */
function repairCachedAgentClassification(blocks: readonly Block[]): Block[] {
  const parentTools = new Map<string, ToolBlock>();
  for (const block of blocks) {
    if (block.kind === "tool") parentTools.set(block.tool_use_id, block);
  }
  return blocks.map((block) => {
    if (block.kind !== "process" || block.processKind !== "agent"
        || !block.parent_id) return block;
    const parent = parentTools.get(block.parent_id);
    if (!parent) return block;
    const tool = parent.tool.toLowerCase();
    if (parent.category === "agent" || tool === "agent" || tool === "task") {
      return block;
    }
    return {
      ...block,
      processKind: "task",
      title: block.title === "协作代理" ? "后台任务" : block.title,
      background: true,
    };
  });
}

/** Paint only heavyweight blocks from a same-revision/generation browser
 * cache over an authoritative summary row.
 *
 * The summary remains authoritative for prompt/final/lifecycle. Cached process
 * is deliberately marked provisional and owns no source segments; the next
 * accepted TurnDetail page replaces it rather than merging stale events into
 * the transcript projection.
 */
function installCachedDetailRestore(
  summary: Turn,
  cached: Turn,
  authority: CachedDetailAuthority,
  activeOwnerId: string | null,
): Turn {
  if (!summary.done || !cached.done || summary.detailLoaded
      || summary.detailProjection
      || statedProcessDetailState(summary) === "none") return summary;
  const source = cached.detailProjection?.blocks ?? cached.blocks;
  // A session-wide running bit is not turn ownership. The next task may have
  // started before its UserMsg/TurnBinding while a same-revision cache row from
  // the completed predecessor is still arriving. Preserve a cached open Plan
  // only when both sides of this exact restore name the current live owner.
  const preserveOpenPlans = authority === "provisional"
    || (authority === "running" && !!activeOwnerId
      && exactTurnAliases(summary).has(activeOwnerId)
      && exactTurnAliases(cached).has(activeOwnerId));
  const blocks = repairCachedAgentClassification(
    source.filter((block) => !isFinalTextBlock(block)),
  ).map((block) => cloneSettledCachedDetailBlock(
    block, summary, preserveOpenPlans));
  if (blocks.length === 0) return summary;
  const processDetailState = mergedProcessDetailState(
    summary, cached, blocks);
  return {
    ...summary,
    processDetailState,
    detailReasons: mergedDetailReasons(
      summary, cached, processDetailState),
    processStartedTs: processDetailState === "present"
      ? earliestTimestamp(
          trustworthyProcessStartedTs(summary),
          trustworthyProcessStartedTs(cached))
      : undefined,
    processDoneTs: processDetailState === "present"
      ? latestTimestamp(
          trustworthyProcessDoneTs(summary),
          trustworthyProcessDoneTs(cached))
      : undefined,
    detailLoaded: false,
    detailLoading: false,
    detailError: undefined,
    detailRetryBefore: undefined,
    detailRetryDirection: undefined,
    detailProjection: {
      // Empty segments distinguish instant-paint cache from authoritative
      // cursor pages. installTurnDetailProjectionPage then replaces this
      // visible block list with the first accepted server page.
      segments: [],
      blocks,
      capped: cached.detailProjection?.capped ?? false,
      hasMore: false,
      oldestCursor: null,
      hasNewer: false,
      newerCursor: null,
    },
    detailHasMore: false,
    detailOldestCursor: null,
    detailHasNewer: false,
    detailNewerCursor: null,
    detailAutoLoad: false,
    // Cache paint is already useful and remains explicitly provisional. Do not
    // turn entering a completed session into a background detail fetch or an
    // automatic disclosure; the user's first click requests the canonical page.
    detailRestorePending: false,
    detailRestoreIncomplete: false,
    detailRestoreOpen: false,
  };
}

/** Keep heavyweight process which this browser painted from the live stream
 * visible across the first authoritative summary after completion/interrupt.
 *
 * This path is intentionally stricter than cache migration: only exact native
 * identity aliases match, and the summary remains authoritative for prompt,
 * final text, lifecycle, and ordering. The caller scopes observedTurns to one
 * accepted history revision/generation, so rollback cannot resurrect a row.
 */
export function restoreObservedLiveTurnDetails(
  summaries: Turn[],
  observedTurns: readonly Turn[],
): Turn[] {
  const observedMatches = new Array<number>(summaries.length).fill(-1);
  const usedObserved = new Set<number>();
  const reserveMatches = (
    predicate: (summary: Turn, observed: Turn) => boolean,
  ) => {
    // A steered Codex task owns several visible user segments which all share
    // one forkPointId. Reserve exact per-segment identities across the complete
    // suffix before that native-task compatibility alias can claim any row.
    // Match newest-to-newest so a legacy projection without exact user ids
    // still preserves the narrative segment order.
    for (let summaryIndex = summaries.length - 1; summaryIndex >= 0;
      summaryIndex -= 1) {
      if (observedMatches[summaryIndex] >= 0) continue;
      for (let observedIndex = observedTurns.length - 1;
        observedIndex >= 0; observedIndex -= 1) {
        if (usedObserved.has(observedIndex)
            || !predicate(summaries[summaryIndex], observedTurns[observedIndex])) {
          continue;
        }
        observedMatches[summaryIndex] = observedIndex;
        usedObserved.add(observedIndex);
        break;
      }
    }
  };
  reserveMatches(sharesExactTurnAlias);
  reserveMatches(sameTurnIdentity);

  const restored = [...summaries];
  for (let summaryIndex = restored.length - 1; summaryIndex >= 0;
    summaryIndex -= 1) {
    const summary = restored[summaryIndex];
    const observedIndex = observedMatches[summaryIndex];
    if (observedIndex < 0) continue;
    const observed = observedTurns[observedIndex];
    if (!summary.done || !observed.done || summary.detailLoaded
        || summary.detailProjection) continue;
    const sourceWithArchive = mergeDetailWithLiveTail(
      observed.detailProjection?.blocks ?? [],
      observed.liveSpillBlocks ?? [],
      true,
    );
    const source = mergeDetailWithLiveTail(
      sourceWithArchive,
      observed.blocks,
      true,
    );
    const blocks = source.filter((block) => !isFinalTextBlock(block)
        && (block.kind !== "text" || block.text.length > 0))
      .map(cloneDetailBlock);
    if (blocks.length === 0) continue;
    const processDetailState = mergedProcessDetailState(
      summary, observed, blocks);
    restored[summaryIndex] = {
      ...summary,
      processDetailState,
      detailReasons: mergedDetailReasons(
        summary, observed, processDetailState),
      processStartedTs: processDetailState === "present"
        ? earliestTimestamp(
            trustworthyProcessStartedTs(summary),
            trustworthyProcessStartedTs(observed))
        : undefined,
      processDoneTs: processDetailState === "present"
        ? latestTimestamp(
            trustworthyProcessDoneTs(summary),
            trustworthyProcessDoneTs(observed))
        : undefined,
      detailLoaded: false,
      detailLoading: false,
      detailError: undefined,
      detailProjection: {
        segments: [],
        blocks,
        capped: observed.detailProjection?.capped ?? false,
        hasMore: false,
        oldestCursor: null,
        hasNewer: false,
        newerCursor: null,
      },
      detailHasMore: false,
      detailOldestCursor: null,
      detailHasNewer: false,
      detailNewerCursor: null,
      detailAutoLoad: false,
      detailRestorePending: false,
      detailRestoreIncomplete: false,
      detailRestoreOpen: false,
    };
  }
  return restored;
}

/** Reconcile cached process with canonical summary identities without
 * resurrecting a completed row which authoritative History removed. The
 * authority describes the enclosing native task, not the summary row's own
 * completed bit: a neutral steer row may be done while its Plan remains live. */
export function restoreCachedTurnDetails(
  summaries: Turn[],
  cachedTurns: readonly Turn[],
  authority: CachedDetailAuthority,
  activeOwnerId: string | null = null,
): Turn[] {
  const matches = new Array<number>(summaries.length).fill(-1);
  const usedCached = new Set<number>();

  const reserveMatches = (
    predicate: (summary: Turn, cached: Turn) => boolean,
  ) => {
    for (let summaryIndex = summaries.length - 1; summaryIndex >= 0;
      summaryIndex -= 1) {
      if (matches[summaryIndex] >= 0) continue;
      for (let cachedIndex = cachedTurns.length - 1;
        cachedIndex >= 0; cachedIndex -= 1) {
        if (usedCached.has(cachedIndex)
            || !predicate(summaries[summaryIndex], cachedTurns[cachedIndex])) {
          continue;
        }
        matches[summaryIndex] = cachedIndex;
        usedCached.add(cachedIndex);
        break;
      }
    }
  };
  // A steered native task gives every visible segment the same forkPointId.
  // Reserve exact user/client identities for the full suffix before using that
  // broad compatibility alias, otherwise one segment can consume another's
  // cached process and make the original tools appear to vanish.
  reserveMatches(sharesExactTurnAlias);
  reserveMatches(sameTurnIdentity);

  // Older optimistic caches may legitimately have a different id. Keep that
  // compatibility one-to-one and choose the closest timestamp so repeated
  // prompts cannot all reuse the first eligible cache row.
  for (let summaryIndex = 0; summaryIndex < summaries.length; summaryIndex += 1) {
    if (matches[summaryIndex] >= 0) continue;
    const summary = summaries[summaryIndex];
    let bestIndex = -1;
    let bestDistance = Number.POSITIVE_INFINITY;
    for (let cachedIndex = 0; cachedIndex < cachedTurns.length;
      cachedIndex += 1) {
      if (usedCached.has(cachedIndex)) continue;
      const candidate = cachedTurns[cachedIndex];
      if (!sameLegacyCachedTurn(summary, candidate)) continue;
      const distance = Math.abs((summary.ts ?? 0) - (candidate.ts ?? 0));
      if (distance >= bestDistance) continue;
      bestIndex = cachedIndex;
      bestDistance = distance;
    }
    if (bestIndex < 0) continue;
    matches[summaryIndex] = bestIndex;
    usedCached.add(bestIndex);
  }

  const restored = [...summaries];
  for (let summaryIndex = restored.length - 1; summaryIndex >= 0;
    summaryIndex -= 1) {
    const cachedIndex = matches[summaryIndex];
    if (cachedIndex < 0) continue;
    const summary = restored[summaryIndex];
    const installed = installCachedDetailRestore(
      summary, cachedTurns[cachedIndex], authority, activeOwnerId);
    const error = historicalInterruptionError(summary, cachedTurns[cachedIndex]);
    restored[summaryIndex] = error === installed.error
      ? installed : { ...installed, error };
  }
  return restored;
}

function mergeTurn(
  history: Turn,
  live: Turn,
  preserveLiveOpen = false,
  completedTextAuthority: "first" | "second" | "combine" = "first",
  settledCanonicalText = false,
  preserveCompleteLiveOrder = false,
): Turn {
  const historyImageRefs = history.imageRefs?.length
    ? history.imageRefs : undefined;
  // A matched transcript row keeps its native lookup identity even when the
  // visible row adopts an optimistic browser id. Detail and image reads both
  // target that history id; tying it only to imageRefs makes an attachment-free
  // alias lose GetTurnDetail authority after the merge.
  const historyTurnId = history.historyTurnId
    ?? (history.id !== live.id ? history.id : live.historyTurnId);
  const detailProjection = live.detailProjection ?? history.detailProjection;
  const terminalSource = live.terminalSource ?? history.terminalSource;
  // preserveLiveOpen only says that a live projection overlaps the newest
  // source row. It is not authority to erase a real interrupted/failed source
  // terminal. Compact continuation is proven later from the wrapper's exact
  // native-id fence and may then reopen that terminal deliberately.
  const liveOwnsLifecycle = preserveLiveOpen
    && history.interrupted !== true
    && history.error == null;
  const blocks = mergeBlocks(
    history.blocks,
    live.blocks,
    preserveLiveOpen,
    false,
    true,
    completedTextAuthority,
    settledCanonicalText,
  );
  const processDetailState = mergedProcessDetailState(
    history, live, blocks);
  const detailReasons = mergedDetailReasons(
    history, live, processDetailState);
  // A Plan is durable session-level progress and ChatView may lift it into the
  // composer-adjacent progress strip.  It therefore cannot prove that this
  // turn's heavyweight process projection is still resident: treating a lone
  // Plan as loaded detail leaves no inline process after that lift and hides
  // the server-advertised collapsed "已处理" affordance.
  const hasLoadedDetailPayload = blocks.some((block) =>
    !isFinalTextBlock(block)
    && (block as ProcessBlock).processKind !== "plan");
  if (preserveCompleteLiveOrder) {
    const historyIds = history.blocks.map(blockIdentity);
    const liveOrders = live.blocks.map((block) => block.liveOrder);
    const liveIds = new Set(live.blocks.map(blockIdentity));
    const mergedIds = blocks.map(blockIdentity);
    if (liveOrders.every((order): order is number => Number.isFinite(order))
        && new Set(liveOrders).size === live.blocks.length
        && liveIds.size === live.blocks.length
        && new Set(historyIds).size === historyIds.length
        && new Set(mergedIds).size === mergedIds.length
        && historyIds.every((identity) => liveIds.has(identity))) {
      const byId = new Map(blocks.map((block) => [blockIdentity(block), block]));
      blocks.splice(0, blocks.length, ...[...live.blocks]
        .sort((left, right) => left.liveOrder! - right.liveOrder!)
        .flatMap((block) => byId.get(blockIdentity(block)) ?? []));
    }
  }
  return {
    ...history,
    id: live.id,
    clientMsgId: history.clientMsgId ?? live.clientMsgId,
    historyTurnId,
    forkPointId: history.forkPointId ?? live.forkPointId,
    checkpointId: history.checkpointId ?? live.checkpointId,
    prompt: history.prompt || live.prompt,
    blocks,
    // A transcript has no ResultMessage, so its EOF is represented by a
    // synthetic TurnEnd.  While this same live tail is still running, that
    // marker is only a snapshot boundary and must not close the turn early.
    done: liveOwnsLifecycle ? live.done : history.done || live.done,
    interrupted: liveOwnsLifecycle
      ? live.interrupted : history.interrupted || live.interrupted,
    error: live.error ?? history.error,
    ...(terminalSource ? { terminalSource } : {}),
    progress: liveOwnsLifecycle ? live.progress : undefined,
    // A summary page replaces optimistic inline image bodies with canonical,
    // payload-free references. Retaining both makes ChatView lay out the same
    // attachment twice and leaves a large placeholder below the visible image.
    images: historyImageRefs ? undefined : live.images ?? history.images,
    imageRefs: historyImageRefs ?? live.imageRefs ?? history.imageRefs,
    files: live.files ?? history.files,
    fileChanges: liveOwnsLifecycle ? live.fileChanges ?? history.fileChanges : history.fileChanges ?? live.fileChanges,
    fileChangesTurnId: liveOwnsLifecycle
      ? live.fileChangesTurnId ?? history.fileChangesTurnId
      : history.fileChangesTurnId ?? live.fileChangesTurnId,
    ts: Math.min(history.ts ?? Number.MAX_SAFE_INTEGER,
      live.ts ?? Number.MAX_SAFE_INTEGER) === Number.MAX_SAFE_INTEGER
      ? undefined
      : Math.min(history.ts ?? Number.MAX_SAFE_INTEGER,
          live.ts ?? Number.MAX_SAFE_INTEGER),
    doneTs: liveOwnsLifecycle
      ? live.doneTs
      : Math.max(history.doneTs ?? 0, live.doneTs ?? 0) || undefined,
    durationMs: history.durationMs === 0 && (live.durationMs ?? 0) > 0
      ? live.durationMs
      : history.durationMs ?? live.durationMs,
    processDetailState,
    detailReasons,
    processStartedTs: processDetailState === "present"
      ? earliestTimestamp(
          trustworthyProcessStartedTs(history),
          trustworthyProcessStartedTs(live))
      : undefined,
    processDoneTs: processDetailState === "present"
      ? latestTimestamp(
          trustworthyProcessDoneTs(history),
          trustworthyProcessDoneTs(live))
      : undefined,
    // Detail is a monotonic, revision-bound local projection. A later summary
    // may legitimately contain no heavyweight blocks; it must not erase pages
    // which the user already expanded in this same revision.
    detailProjection,
    // A summary which still advertises deferred events cannot inherit a stale
    // `detailLoaded=true` bit after the corresponding browser projection was
    // evicted or invalidated.  That contradictory state hides the collapsed
    // process affordance entirely (the final answer remains, but "已处理"
    // disappears).  Keep loaded monotonic only while there is an actual detail
    // projection or visible turn-detail payload to justify it. A standalone
    // Plan is intentionally not such payload because it renders outside the
    // turn timeline.
    detailLoaded: !!(detailProjection
      || (live.detailLoaded || history.detailLoaded)
        && (hasLoadedDetailPayload || !hasDeferredTurnDetail(history))),
    detailLoading: live.detailLoading ?? history.detailLoading,
    detailError: live.detailError ?? history.detailError,
    detailResetPending:
      live.detailResetPending ?? history.detailResetPending,
    detailHasMore: detailProjection
      ? detailProjection.hasMore
      : live.detailHasMore ?? history.detailHasMore,
    detailOldestCursor: detailProjection
      ? detailProjection.oldestCursor
      : live.detailOldestCursor ?? history.detailOldestCursor,
    detailHasNewer: detailProjection
      ? detailProjection.hasNewer
      : live.detailHasNewer ?? history.detailHasNewer,
    detailNewerCursor: detailProjection
      ? detailProjection.newerCursor
      : live.detailNewerCursor ?? history.detailNewerCursor,
    detailAutoLoad: live.detailAutoLoad ?? history.detailAutoLoad,
    liveBlocksSpilled:
      live.liveBlocksSpilled ?? history.liveBlocksSpilled,
    liveSpilledBlockCount: Math.max(
      live.liveSpilledBlockCount ?? 0,
      history.liveSpilledBlockCount ?? 0,
    ) || undefined,
    liveSpillBlocks: live.liveSpillBlocks ?? history.liveSpillBlocks,
    liveSpillRefreshCount: Math.max(
      live.liveSpillRefreshCount ?? 0,
      history.liveSpillRefreshCount ?? 0,
    ) || undefined,
  };
}

function restoreAuthoritativeLifecycle(merged: Turn, history: Turn, live: Turn): Turn {
  const restored = {
    ...merged,
    done: history.done,
    interrupted: history.interrupted,
    error: historicalInterruptionError(history, live),
    progress: history.progress,
    doneTs: history.doneTs,
    durationMs: history.durationMs,
  };
  if (history.terminalSource) restored.terminalSource = history.terminalSource;
  else delete restored.terminalSource;
  return restored;
}

function restoreUnownedHistoryIdentity(
  merged: Turn,
  history: Turn,
  live: Turn,
): Turn {
  // A browser-owned optimistic row carries clientMsgId and deliberately keeps
  // its stable React id after the native History id materializes. A prompt-less
  // generation orphan has no such owner. Older bundles may already have copied
  // History's prompt and historyTurnId onto that synthetic id, so an idle Codex
  // page must canonicalize it again or the polluted id survives every refresh.
  if (live.id === history.id || live.clientMsgId
      || live.historyTurnId !== history.id) return merged;
  const restored = { ...merged, id: history.id };
  if (history.historyTurnId) restored.historyTurnId = history.historyTurnId;
  else delete restored.historyTurnId;
  return restored;
}

/** Merge previously-loaded heavyweight detail into a newer summary without
 * allowing stale detail lifecycle fields to reopen a steered/completed turn. */
export function mergeAuthoritativeTurnDetail(
  summary: Turn,
  detail: Turn,
): Turn {
  // The summary owns lifecycle and ordering, but an already-loaded detail page
  // owns the complete payload for an exact native message. In particular, a
  // later summary refresh must not replace a >256 KiB final answer with the
  // bounded "expand for full content" prefix again.
  const merged = mergeTurn(summary, detail, false, "second");
  return {
    ...merged,
    id: summary.id,
    done: summary.done,
    doneTs: summary.doneTs,
    durationMs: summary.durationMs,
    interrupted: summary.interrupted,
    error: summary.error,
    progress: summary.progress,
    detailEventCount: summary.detailEventCount,
    detailLoaded: detail.detailLoaded ?? true,
    detailLoading: false,
    detailError: undefined,
    detailRetryBefore: undefined,
    detailRetryDirection: undefined,
    detailResetPending: false,
    detailProjection: detail.detailProjection ?? summary.detailProjection,
    detailHasMore: detail.detailProjection
      ? detail.detailProjection.hasMore
      : detail.detailHasMore ?? summary.detailHasMore,
    detailOldestCursor: detail.detailProjection
      ? detail.detailProjection.oldestCursor
      : detail.detailOldestCursor ?? summary.detailOldestCursor,
    detailHasNewer: detail.detailProjection
      ? detail.detailProjection.hasNewer
      : detail.detailHasNewer ?? summary.detailHasNewer,
    detailNewerCursor: detail.detailProjection
      ? detail.detailProjection.newerCursor
      : detail.detailNewerCursor ?? summary.detailNewerCursor,
    detailAutoLoad: detail.detailAutoLoad ?? summary.detailAutoLoad,
    detailRestorePending: false,
    detailRestoreOpen:
      detail.detailRestoreOpen ?? summary.detailRestoreOpen,
    detailRestoreIncomplete:
      detail.detailRestoreIncomplete ?? summary.detailRestoreIncomplete,
  };
}

/** Install one bounded intra-turn detail page.
 *
 * Pages are source-disjoint and may be visited in either direction, so the
 * visible process window is replaced instead of accumulated. Keep the summary's
 * final answer and generated output images outside that window when a page
 * does not contain them. */
export function installAuthoritativeTurnDetailPage(
  summary: Turn,
  detail: Turn,
  page: {
    hasMore: boolean;
    oldestCursor?: string | null;
    hasNewer: boolean;
    newerCursor?: string | null;
  },
  projection?: TurnDetailProjection,
): Turn {
  const summaryFinals = summary.blocks.filter(isFinalTextBlock);
  const pageFinals = detail.blocks.filter(isFinalTextBlock);

  const alignFinalSegment = (
    summarySegment: TextBlock[],
    pageSegment: TextBlock[],
  ): TextBlock[] => {
    if (pageSegment.length === 0) return summarySegment;
    // Without an exact id inside this source-ordered segment, older caches may
    // have regenerated assistant ids. Align from the end: a one-to-one alias
    // replaces instead of duplicating, while an unvisited summary prefix stays
    // visible and page-only leading finals remain in source order.
    if (pageSegment.length >= summarySegment.length) return pageSegment;
    return [
      ...summarySegment.slice(0, summarySegment.length - pageSegment.length),
      ...pageSegment,
    ];
  };

  const summaryIndexById = new Map(
    summaryFinals.map((block, index) => [block.message_id, index] as const),
  );
  const anchors: Array<{ summaryIndex: number; pageIndex: number }> = [];
  let nextSummaryIndex = 0;
  pageFinals.forEach((block, pageIndex) => {
    const summaryIndex = summaryIndexById.get(block.message_id);
    if (summaryIndex == null || summaryIndex < nextSummaryIndex) return;
    anchors.push({ summaryIndex, pageIndex });
    nextSummaryIndex = summaryIndex + 1;
  });

  const canonicalFinals: TextBlock[] = [];
  let summaryStart = 0;
  let pageStart = 0;
  for (const anchor of anchors) {
    canonicalFinals.push(...alignFinalSegment(
      summaryFinals.slice(summaryStart, anchor.summaryIndex),
      pageFinals.slice(pageStart, anchor.pageIndex),
    ));
    // The detail page is authoritative for an exact native message id.
    canonicalFinals.push(pageFinals[anchor.pageIndex]);
    summaryStart = anchor.summaryIndex + 1;
    pageStart = anchor.pageIndex + 1;
  }
  canonicalFinals.push(...alignFinalSegment(
    summaryFinals.slice(summaryStart),
    pageFinals.slice(pageStart),
  ));
  const detailWithoutFinals = detail.blocks.filter(
    (block) => !isFinalTextBlock(block));
  const canonicalImageRefs = detail.imageRefs ?? summary.imageRefs;
  const detailProjection = projection ?? summary.detailProjection;
  const hasMore = detailProjection
    ? detailProjection.hasMore : page.hasMore;
  const oldestCursor = detailProjection
    ? detailProjection.oldestCursor : page.oldestCursor ?? null;
  const hasNewer = detailProjection
    ? detailProjection.hasNewer : page.hasNewer;
  const newerCursor = detailProjection
    ? detailProjection.newerCursor : page.newerCursor ?? null;
  const restoreIncomplete =
    summary.detailRestoreIncomplete === true && hasMore;
  const processBlocks = detailProjection?.blocks ?? detailWithoutFinals;
  // Output images are bounded narrative assets, not members of the currently
  // selected heavy-process page. Expanding or paging details must not make
  // their gallery disappear again. Retain references only, never image bytes.
  const outputImages = generatedOutputImages([...summary.blocks, ...processBlocks]);
  const detailImageIds = new Set(generatedOutputImages(detailWithoutFinals)
    .map(generatedImageIdentity));
  let processDetailState = mergedProcessDetailState(
    summary, detail, processBlocks);
  // A bounded page containing only the final answer is not an exact
  // process-free conclusion while adjacent source pages remain unread. Keep
  // the honest unknown state mounted so automatic/manual pagination cannot
  // make its detail affordance blink out between responses.
  if (processDetailState === "none"
      && statedProcessDetailState(summary) === "unknown"
      && (hasMore || hasNewer)) {
    processDetailState = "unknown";
  }
  const incompleteUnknownProcess = processDetailState === "unknown"
    && (hasMore || hasNewer);
  const retryDirection = hasMore ? "older" : hasNewer ? "newer" : undefined;
  const retryBefore = hasMore ? oldestCursor : hasNewer ? newerCursor : null;
  return {
    ...summary,
    prompt: detail.prompt || summary.prompt,
    images: canonicalImageRefs?.length
      ? undefined : detail.images ?? summary.images,
    imageRefs: canonicalImageRefs,
    files: detail.files ?? summary.files,
    // Heavy process/tool/commentary pages live in detailProjection so the
    // ordinary live-turn 256 item / 16 MiB cap cannot evict them. Legacy
    // callers without a projection retain the pre-v21 behavior.
    blocks: detailProjection
      ? [...outputImages, ...canonicalFinals]
      : [...detailWithoutFinals,
          ...outputImages.filter(image => !detailImageIds.has(generatedImageIdentity(image))),
          ...canonicalFinals],
    done: summary.done,
    doneTs: summary.doneTs,
    durationMs: summary.durationMs,
    processDetailState,
    detailReasons: mergedDetailReasons(
      summary, detail, processDetailState),
    processStartedTs: processDetailState === "present"
      ? earliestTimestamp(
          trustworthyProcessStartedTs(summary),
          trustworthyProcessStartedTs(detail))
      : undefined,
    processDoneTs: processDetailState === "present"
      ? latestTimestamp(
          trustworthyProcessDoneTs(summary),
          trustworthyProcessDoneTs(detail))
      : undefined,
    interrupted: summary.interrupted,
    error: summary.error,
    progress: summary.progress,
    detailEventCount: summary.detailEventCount,
    detailLoaded: !restoreIncomplete && !incompleteUnknownProcess,
    detailLoading: false,
    detailError: undefined,
    detailRetryBefore: incompleteUnknownProcess ? retryBefore : undefined,
    detailRetryDirection: incompleteUnknownProcess
      ? retryDirection : undefined,
    detailResetPending: false,
    detailProjection,
    detailHasMore: hasMore,
    detailOldestCursor: oldestCursor,
    detailHasNewer: hasNewer,
    detailNewerCursor: newerCursor,
    detailAutoLoad:
      !!summary.detailAutoLoad && hasMore && !detailProjection?.capped,
    detailRestorePending: false,
    detailRestoreIncomplete: restoreIncomplete,
    liveSpillBlocks: summary.done && !hasMore && !hasNewer
      ? undefined : summary.liveSpillBlocks,
  };
}

function chronologicalTurnTime(turn: Turn): number | undefined {
  if (turn.prompt || turn.doneTs == null) return turn.ts;
  const terminalStart = Math.max(0, turn.doneTs - (turn.durationMs ?? 0));
  // Older caches and mixed-version wrappers may contain replay-generated
  // assistant-only starts stamped after their authoritative terminal. Use the
  // terminal-derived time for ordering without mutating the rendered payload.
  if (turn.ts == null || turn.ts > turn.doneTs) return terminalStart;
  return turn.ts;
}

/** Merge transcript history with cache/live state without deleting a just-finished
 * turn that hasn't flushed yet or duplicating the same prompt under engine ids. */
export function mergeInitialHistory(
  history: Turn[],
  live: Turn[],
  options: {
    preserveLiveTailOpen?: boolean;
    /** Exact visible row named as the newest authoritative History row.
     * This binds a uniquely proven live block overlap while a steered row's
     * client/native user alias is still materializing. It also lets a terminal
     * page collapse the stale empty active shell left by an earlier snapshot. */
    newestHistoryId?: string | null;
    /** Exact ordered live owner from TurnBinding/liveOwner. Array position is
     * not ownership evidence after cache hydration and replay interleave. */
    activeOwnerId?: string | null;
    /** A newest authoritative page may absorb a replay-created assistant row
     * only when native block identities prove that row belongs to one
     * canonical turn. Disabled for cache, pagination, re-key and detail merges. */
    reconcileReplayOrphans?: boolean;
  } = {},
  /** A settled Codex app-server History row owns the terminal lifecycle.
   * This positional internal flag avoids shipping another merge-option key. */
  settledCodex = false,
): Turn[] {
  const merged = history.map((turn) => ({ ...turn, blocks: turn.blocks.map((b) => ({ ...b })) }));
  const matches = new Array<number>(live.length).fill(-1);
  const used = new Set<number>();
  const unmatched: Turn[] = [];

  const reserveMatches = (
    predicate: (historyTurn: Turn, liveTurn: Turn) => boolean,
  ) => {
    // Live is generally the newest suffix of History. Match from the tail so
    // repeated steer prompts within one native task bind their newest segment.
    for (let liveIndex = live.length - 1; liveIndex >= 0; liveIndex -= 1) {
      if (matches[liveIndex] >= 0) continue;
      for (let historyIndex = merged.length - 1; historyIndex >= 0;
        historyIndex -= 1) {
        if (used.has(historyIndex)
            || !predicate(merged[historyIndex], live[liveIndex])) continue;
        matches[liveIndex] = historyIndex;
        used.add(historyIndex);
        break;
      }
    }
  };

  // Reserve exact identities for the whole live suffix before the guarded
  // compaction fallback can consume a row belonging to another live segment.
  reserveMatches((historyTurn, liveTurn) => historyTurn.id === liveTurn.id);
  reserveMatches(sharesExactTurnAlias);

  const itemOwners = new Map<string, Set<number>>();
  history.forEach((turn, index) => {
    for (const block of turn.blocks) {
      const key = blockIdentity(block);
      const owners = itemOwners.get(key) ?? new Set<number>();
      owners.add(index);
      itemOwners.set(key, owners);
    }
  });
  // A steer and its predecessor share a native task. Reserve exact item
  // ownership before considering that coarse alias, including an old live row
  // whose user-message binding has already fallen out of the replay ring.
  const nativeItemOwners = new Map(live.map((turn) => {
    const owners = new Set(turn.blocks.flatMap((block) =>
      [...(itemOwners.get(blockIdentity(block)) ?? [])]));
    return [turn, owners.size === 1 ? [...owners][0] : -1] as const;
  }));
  const historyIndexes = new Map(merged.map((turn, index) => [turn, index]));
  reserveMatches((historyTurn, liveTurn) => {
    const nativeId = nativeTaskIdentity(historyTurn);
    if (!nativeId || nativeId !== nativeTaskIdentity(liveTurn)) return false;
    return nativeItemOwners.get(liveTurn) === historyIndexes.get(historyTurn);
  });
  reserveMatches(sharesCompactionTurnAlias);
  const historyAliasCounts = new Map<Turn, number>();
  const liveAliasCounts = new Map<Turn, number>();
  reserveMatches((historyTurn, liveTurn) => sameTurn(historyTurn, liveTurn)
    // A task-only fallback is safe only when it identifies one row on both
    // sides. Even identical prompts can represent distinct user steers.
    && (historyAliasCounts.get(liveTurn) ?? historyAliasCounts.set(liveTurn,
      history.filter((candidate) => sameTurn(candidate, liveTurn)).length
    ).get(liveTurn)) === 1
    && (liveAliasCounts.get(historyTurn) ?? liveAliasCounts.set(historyTurn,
      live.filter((candidate) => sameTurn(historyTurn, candidate)).length
    ).get(historyTurn)) === 1);

  const explicitActiveOwnerIndexes = options.activeOwnerId
    ? live.flatMap((turn, index) =>
        !turn.done && exactTurnAliases(turn).has(options.activeOwnerId!)
          ? [index] : [])
    : [];
  const activeOwnerIndex = explicitActiveOwnerIndexes.length === 1
    ? explicitActiveOwnerIndexes[0] : live.length - 1;

  const replayOrphanMatches = new Set<number>();
  const activeReplayMatches = new Set<number>();
  const authoritativeHeadDuplicateMatches = new Set<number>();
  if (options.reconcileReplayOrphans) {
    const owners = new Map<string, Set<number>>();
    const addOwner = (key: string, historyIndex: number) => {
      const indexes = owners.get(key) ?? new Set<number>();
      indexes.add(historyIndex);
      owners.set(key, indexes);
    };
    const blockKeys = (block: Block): string[] => {
      if (block.kind === "text") return [`message:${block.message_id}`];
      if (block.kind === "tool") {
        return [
          `message:${block.message_id}`,
          `tool:${block.tool_use_id}`,
        ];
      }
      return [`process:${block.item_id}`];
    };
    merged.forEach((turn, historyIndex) => {
      for (const block of turn.blocks) {
        for (const key of blockKeys(block)) addOwner(key, historyIndex);
      }
    });
    const authoritativeHistoryIndexes = options.newestHistoryId
      ? merged.flatMap((turn, index) =>
          exactTurnAliases(turn).has(options.newestHistoryId!)
            ? [index] : [])
      : [];
    const authoritativeHistoryIndex = authoritativeHistoryIndexes.length === 1
      ? authoritativeHistoryIndexes[0] : -1;
    const primaryBlockKey = (block: Block): string => block.kind === "text"
      ? `message:${block.message_id}`
      : block.kind === "tool"
        ? `tool:${block.tool_use_id}`
        : `process:${block.item_id}`;

    const claims = new Map<number, number[]>();
    for (let liveIndex = 0; liveIndex < live.length; liveIndex += 1) {
      if (matches[liveIndex] >= 0) continue;
      const candidate = live[liveIndex];
      const candidatePrimaryKeyList = candidate.blocks.map(primaryBlockKey);
      const candidatePrimaryKeys = [
        ...new Set(candidatePrimaryKeyList),
      ];
      const activeCandidate = authoritativeHistoryIndex >= 0
        && !candidate.done;
      const exactActiveProjection = activeCandidate
        && candidatePrimaryKeys.length > 0
        && candidatePrimaryKeys.length === candidatePrimaryKeyList.length
        && candidatePrimaryKeys.every((key) => {
          const keyOwners = owners.get(key);
          return keyOwners?.size === 1
            && keyOwners.has(authoritativeHistoryIndex);
        });
      if (!activeCandidate && (candidate.prompt
          || candidate.images?.length || candidate.imageRefs?.length
          || candidate.files?.length
          || candidate.clientMsgId || candidate.historyTurnId
          || candidate.forkPointId || candidate.checkpointId
          || candidate.codexTurnId || candidate.liveTaskId
          || candidate.detailProjection
          || candidate.liveSpillBlocks?.length
          || candidate.blocks.length === 0)) continue;
      let historyIndex: number;
      if (activeCandidate) {
        historyIndex = authoritativeHistoryIndex;
        const historyTurn = merged[historyIndex];
        const historyNativeId = nativeTaskIdentity(historyTurn);
        const candidateNativeId = nativeTaskIdentity(candidate);
        if (candidateNativeId && historyNativeId
            && candidateNativeId !== historyNativeId) continue;
        // The physical tail is eligible with one shared native block because
        // no later row can own current replay output. An older unfinished row
        // may only join when every stable block id belongs uniquely to this
        // canonical History turn; this is the safe self-heal path for an
        // IndexedDB row which replay refreshed before TurnBinding created the
        // current owner separately.
        if (liveIndex !== activeOwnerIndex && !exactActiveProjection) continue;
        const shared = candidatePrimaryKeys.some((key) =>
          owners.get(key)?.has(historyIndex));
        const conflicts = candidatePrimaryKeys.some((key) => {
          const keyOwners = owners.get(key);
          return keyOwners != null && (
            keyOwners.size !== 1 || !keyOwners.has(historyIndex)
          );
        });
        if (!shared || conflicts) continue;
      } else {
        const messageOwners = owners.get(`message:${candidate.id}`);
        if (!messageOwners || messageOwners.size !== 1) continue;
        historyIndex = [...messageOwners][0];
      }
      if (used.has(historyIndex) && !exactActiveProjection) continue;
      if (!activeCandidate) {
        // Several tool calls assembled from one assistant item legitimately
        // share its message id. Treat that repeated envelope id as corroborating
        // evidence, not as two provisional rows claiming the same native item.
        // The per-block primary ids must still be unique so genuinely duplicated
        // text/tool/process blocks cannot be hidden by this reconciliation.
        const primaryKeys = candidate.blocks.map(primaryBlockKey);
        const keys = [...new Set(candidate.blocks.flatMap(blockKeys))];
        if (primaryKeys.length === 0
            || new Set(primaryKeys).size !== primaryKeys.length
            || !keys.every((key) => {
          const keyOwners = owners.get(key);
          return keyOwners?.size === 1 && keyOwners.has(historyIndex);
        })) continue;
      }
      const candidates = claims.get(historyIndex) ?? [];
      candidates.push(liveIndex);
      claims.set(historyIndex, candidates);
      if (activeCandidate && liveIndex === activeOwnerIndex) {
        activeReplayMatches.add(liveIndex);
      }
    }
    for (const [historyIndex, liveIndexes] of claims) {
      // Two provisional rows claiming one native assistant item are ambiguous;
      // do not absorb either unless each unfinished projection consists only
      // of stable blocks uniquely owned by this canonical History row. That
      // stronger proof repairs a cache+replay duplicate without ever comparing
      // prompt text, timestamps, or visible prose.
      const safeLiveIndexes = liveIndexes.filter((liveIndex) => {
        const candidate = live[liveIndex];
        const primaryKeys = [
          ...new Set(candidate.blocks.map(primaryBlockKey)),
        ];
        return !candidate.done && primaryKeys.length > 0
          && primaryKeys.length === candidate.blocks.length
          && primaryKeys.every((key) => {
            const keyOwners = owners.get(key);
            return keyOwners?.size === 1 && keyOwners.has(historyIndex);
          });
      });
      const safeLiveIndexSet = new Set(safeLiveIndexes);
      const everyClaimIsProven = liveIndexes.every((liveIndex) =>
        safeLiveIndexSet.has(liveIndex)
        || (liveIndex === activeOwnerIndex
          && activeReplayMatches.has(liveIndex)));
      if (liveIndexes.length !== 1 && !everyClaimIsProven) continue;
      if (used.has(historyIndex)
          && safeLiveIndexes.length !== liveIndexes.length) continue;
      for (const liveIndex of liveIndexes) {
        matches[liveIndex] = historyIndex;
        replayOrphanMatches.add(liveIndex);
      }
      used.add(historyIndex);
    }

    // An item-free active snapshot cannot safely bind a local steer: several
    // visible steer segments may share one native task id. It therefore leaves
    // an exact authoritative shell plus the accepted local row until native
    // blocks arrive. At the terminal snapshot those exact block ids finally
    // prove that the two local rows describe one newest history row. Allow only
    // that unique tail candidate to join the already-reserved head; a shared
    // task id, prompt, timestamp or display text never qualifies on its own.
    if (authoritativeHistoryIndex >= 0
        && matches.some((index) => index === authoritativeHistoryIndex)) {
      const historyTurn = merged[authoritativeHistoryIndex];
      const duplicateCandidates: number[] = [];
      for (let liveIndex = 0; liveIndex < live.length; liveIndex += 1) {
        if (matches[liveIndex] >= 0 || liveIndex !== activeOwnerIndex) continue;
        const candidate = live[liveIndex];
        if (candidate.blocks.length === 0) continue;
        const historyNativeId = nativeTaskIdentity(historyTurn);
        const candidateNativeId = nativeTaskIdentity(candidate);
        if (historyNativeId && candidateNativeId
            && historyNativeId !== candidateNativeId) continue;
        const keys = [...new Set(candidate.blocks.map(primaryBlockKey))];
        const shared = keys.some((key) =>
          owners.get(key)?.has(authoritativeHistoryIndex));
        const conflicts = keys.some((key) => {
          const keyOwners = owners.get(key);
          return keyOwners != null && (
            keyOwners.size !== 1
            || !keyOwners.has(authoritativeHistoryIndex)
          );
        });
        if (shared && !conflicts) duplicateCandidates.push(liveIndex);
      }
      if (duplicateCandidates.length === 1) {
        const liveIndex = duplicateCandidates[0];
        matches[liveIndex] = authoritativeHistoryIndex;
        if (settledCodex
            && isIdleHistoryRestartOrphan(live[liveIndex])) {
          // The fallback row owns no user identity. Keep History's canonical
          // row id/lifecycle and use the uniquely overlapping blocks only as
          // proof that this restart suffix is disposable.
          replayOrphanMatches.add(liveIndex);
        } else {
          authoritativeHeadDuplicateMatches.add(liveIndex);
        }
      }
    }
  }

  // Apply the precomputed mapping in original live order. Matching direction
  // must not reorder unmatched optimistic rows.
  for (let liveIndex = 0; liveIndex < live.length; liveIndex += 1) {
    let liveTurn = live[liveIndex];
    const index = matches[liveIndex];
    if (index >= 0 && (settledCodex || options.reconcileReplayOrphans)) {
      // Heal an older persisted wrong-segment copy only when this canonical
      // page assigns its exact item id uniquely to a different row. Missing
      // summary detail and repeated text are never grounds for deleting items.
      const owned = (block: Block) => {
        const owners = itemOwners.get(blockIdentity(block));
        return !owners || owners.size !== 1 || owners.has(index);
      };
      liveTurn = {
        ...liveTurn,
        blocks: liveTurn.blocks.filter(owned),
        liveSpillBlocks: liveTurn.liveSpillBlocks?.filter(owned),
        detailProjection: liveTurn.detailProjection ? {
          ...liveTurn.detailProjection,
          blocks: liveTurn.detailProjection.blocks.filter(owned),
        } : undefined,
      };
    }
    if (index >= 0) {
      if (authoritativeHeadDuplicateMatches.has(liveIndex)) continue;
      if (activeReplayMatches.has(liveIndex)) {
        const preserveLiveOpen = !!options.preserveLiveTailOpen
          && liveIndex === activeOwnerIndex
          && !liveTurn.done;
        const bound = mergeTurn(
          merged[index], liveTurn, preserveLiveOpen,
        );
        bound.blocks = mergeBlocks(
          merged[index].blocks,
          liveTurn.blocks,
          preserveLiveOpen,
          false,
          false,
        );
        merged[index] = preserveLiveOpen
          ? bound
          : restoreAuthoritativeLifecycle(bound, merged[index], liveTurn);
        continue;
      }
      if (replayOrphanMatches.has(liveIndex)) {
        // History owns row identity, lifecycle and detail authority. Preserve
        // only newer bytes for the exact native blocks proved above; a stale
        // orphan detail failure must never replace canonical GetTurnDetail.
        merged[index] = {
          ...merged[index],
          blocks: mergeBlocks(
            merged[index].blocks,
            liveTurn.blocks,
            false,
            false,
            false,
          ),
        };
        continue;
      }
      const isOpenLiveTail = !!options.preserveLiveTailOpen
        && liveIndex === activeOwnerIndex
        // A newer authoritative history turn proves this local placeholder is
        // no longer the active tail (for example, same-task steering). Only the
        // matching newest history row may inherit an unfinished live state.
        && index === merged.length - 1
        && !liveTurn.done;
      const historyTurn = merged[index];
      const bound = mergeTurn(
        historyTurn, liveTurn, isOpenLiveTail, "first", settledCodex,
        !!options.preserveLiveTailOpen && !!options.reconcileReplayOrphans
          && liveIndex === activeOwnerIndex,
      );
      if (settledCodex && sharesExactTurnAlias(historyTurn, liveTurn)) {
        merged[index] = restoreUnownedHistoryIdentity(
          restoreAuthoritativeLifecycle(bound, historyTurn, liveTurn),
          historyTurn,
          liveTurn,
        );
      } else {
        merged[index] = bound;
      }
    } else {
      unmatched.push({ ...liveTurn, blocks: liveTurn.blocks.map((b) => ({ ...b })) });
    }
  }

  // Apply a proven duplicate only after the ordinary exact match has refreshed
  // the authoritative shell. This makes the accepted live id the stable React
  // row id while preserving the native history id for detail/image requests.
  for (const liveIndex of authoritativeHeadDuplicateMatches) {
    const index = matches[liveIndex];
    if (index < 0) continue;
    const liveTurn = live[liveIndex];
    const preserveLiveOpen = !!options.preserveLiveTailOpen
      && liveIndex === activeOwnerIndex && !liveTurn.done;
    const bound = mergeTurn(merged[index], liveTurn, preserveLiveOpen);
    bound.blocks = mergeBlocks(
      merged[index].blocks,
      liveTurn.blocks,
      preserveLiveOpen,
      false,
      false,
    );
    merged[index] = preserveLiveOpen
      ? bound
      : restoreAuthoritativeLifecycle(bound, merged[index], liveTurn);
  }

  const rows = [...merged, ...unmatched].map((turn, order) => ({ turn, order }));
  rows.sort((a, b) => {
    const aTime = chronologicalTurnTime(a.turn);
    const bTime = chronologicalTurnTime(b.turn);
    if (aTime != null && bTime != null && aTime !== bTime) {
      return aTime - bTime;
    }
    return a.order - b.order;
  });
  const result: Turn[] = [];
  for (const { turn } of rows) {
    const duplicate = result.findIndex((row) =>
      sharesExactTurnAlias(row, turn));
    if (duplicate < 0) {
      result.push(turn);
      continue;
    }
    // The old one-to-one matcher could leave both local ids until this final
    // exact-alias pass. Do not merely discard the later canonical native row:
    // merge it into the optimistic browser row so History blocks/detail survive.
    if (turn.clientMsgId && turn.id !== turn.clientMsgId) {
      result[duplicate] = mergeTurn(turn, result[duplicate]);
    }
  }
  return options.reconcileReplayOrphans
    ? reconcileProvenCompactionOrphans(result) : result;
}
