import type { Block, Turn } from "./domain/conversation";
import type { TurnEnd } from "./protocol";

/** Unread receipts can be cleared, but an already delivered BTW terminal must
 * stay remembered. SyncBtw rebuilds replay the same terminals without seq.
 * Record even visible completions, before deciding whether to show a badge or
 * notification; their later replay must not become a new unread completion.
 */
export class BtwCompletionTracker {
  private readonly sessions = new Map<string, {
    machineId: string;
    completions: Set<string>;
  }>();

  take(machineId: string, sid: string,
    event: Pick<TurnEnd, "turn_id" | "checkpoint_id" | "ts">,
  ): boolean {
    const sessionKey = JSON.stringify([machineId, sid]);
    const session = this.sessions.get(sessionKey) ?? {
      machineId, completions: new Set<string>(),
    };
    // Legacy terminals without native IDs still retain their original timestamp
    // in presentation history. Transport seq cannot identify a rebuilt event.
    const completionKey = event.turn_id || event.checkpoint_id
      ? JSON.stringify([event.turn_id ?? null, event.checkpoint_id ?? null])
      : JSON.stringify(["ts", event.ts]);
    const fresh = !session.completions.has(completionKey);
    session.completions.delete(completionKey);
    session.completions.add(completionKey);
    // Presentation history retains at most 64 turns per BTW. Leave headroom for
    // live terminals and overlapping replays, while bounding long-lived tabs.
    if (session.completions.size > 256) {
      session.completions.delete(session.completions.values().next().value!);
    }
    this.sessions.delete(sessionKey);
    this.sessions.set(sessionKey, session);
    if (this.sessions.size > 128) {
      this.sessions.delete(this.sessions.keys().next().value!);
    }
    return fresh;
  }

  forget(machineId: string, sid: string): void {
    this.sessions.delete(JSON.stringify([machineId, sid]));
  }

  clear(machineId?: string): void {
    if (machineId == null) this.sessions.clear();
    else for (const [key, session] of this.sessions) {
      if (session.machineId === machineId) this.sessions.delete(key);
    }
  }
}

export interface CompletionReceipt {
  main: boolean;
  mainCompletionId: string | null;
  mainTurnEndSeq: number | null;
  mainTurnEndGeneration: string | null;
  btwSids: string[];
}

export type CompletionReceipts = Record<string, CompletionReceipt>;
export type CompletionBadgeKind = "main" | "btw" | "both";
export interface CompletionProjection {
  id: string | null;
  unread: boolean;
  revision: number;
}

export interface TerminalHistoryRepairAttempt {
  key: string;
  attempts: number;
  pending: boolean;
}

export function nextTerminalHistoryRepairAttempt(
  current: TerminalHistoryRepairAttempt | undefined,
  key: string,
  maxAttempts: number,
): TerminalHistoryRepairAttempt | null {
  const attempts = current?.key === key ? current.attempts : 0;
  if ((current?.key === key && current.pending)
      || attempts >= maxAttempts) return null;
  return { key, attempts: attempts + 1, pending: true };
}

export function settleTerminalHistoryRepairAttempt(
  current: TerminalHistoryRepairAttempt | undefined,
): TerminalHistoryRepairAttempt | null {
  if (!current?.pending) return null;
  return { ...current, pending: false };
}

type CompletionRepairTurn = Pick<Turn,
  "id" | "clientMsgId" | "historyTurnId" | "forkPointId"
    | "checkpointId" | "liveTaskId" | "done" | "doneTs"
> & Partial<Pick<Turn, "blocks" | "liveSpillBlocks" | "detailProjection">>;

function blockHasMessageId(block: Block, messageId: string): boolean {
  return (block.kind === "text" || block.kind === "tool")
    && block.message_id === messageId;
}

function turnContainsMessageId(
  turn: CompletionRepairTurn,
  messageId: string,
): boolean {
  return (turn.blocks?.some((block) =>
    blockHasMessageId(block, messageId)) ?? false)
    || (turn.liveSpillBlocks?.some((block) =>
      blockHasMessageId(block, messageId)) ?? false)
    || (turn.detailProjection?.blocks.some((block) =>
      blockHasMessageId(block, messageId)) ?? false);
}

function turnMatchesCompletion(
  turn: CompletionRepairTurn,
  completionId: string,
): boolean {
  return [
    turn.id,
    turn.clientMsgId,
    turn.historyTurnId,
    turn.forkPointId,
    turn.checkpointId,
    turn.liveTaskId,
  ].includes(completionId)
    || turnContainsMessageId(turn, completionId);
}

function hasOpenForegroundProcess(turn: CompletionRepairTurn): boolean {
  const open = (block: Block) => (
    (block.kind === "tool" || block.kind === "process")
    && !block.done
    && !(block.kind === "process" && block.background === true)
  );
  return (turn.blocks?.some(open) ?? false)
    || (turn.liveSpillBlocks?.some(open) ?? false)
    || (turn.detailProjection?.blocks.some(open) ?? false);
}

function turnHasCompletionFooter(turn: CompletionRepairTurn): boolean {
  return turn.done && turn.doneTs != null
    && !hasOpenForegroundProcess(turn);
}

/** Whether a durable native completion still needs canonical History repair.
 *
 * CompletionState is emitted after TurnEnd and survives disconnects, while the
 * browser's bounded live projection may miss that TurnEnd.  A completed row is
 * considered repaired only when the newest matching visible segment carries
 * its completion timestamp and has no stale foreground process. Codex steer
 * segments intentionally share one native task id, so an older completed
 * segment must never consume the terminal intended for a newer open segment.
 *
 * Legacy Claude receipts used the assistant message id. Canonical History can
 * append a later background follow-up and therefore expose a different final
 * assistant id; matching the original message block keeps those persisted
 * receipts compatible without treating an unrelated turn as terminal.
 */
export function completionNeedsHistoryRepair(
  turns: readonly CompletionRepairTurn[],
  completionId: string | null | undefined,
): boolean {
  if (!completionId) return false;
  if (completionId.startsWith("codex-read-")) {
    // Native read revisions are presentation receipts, not turn terminals.
    // A completed-result receipt carries its exact native turn after the hash;
    // a manually unread thread (or already-read row) has no terminal to repair.
    const native = /^codex-read-[a-f0-9]{64}:([A-Za-z0-9._@-]{1,128})$/.exec(completionId);
    if (!native) return false;
    completionId = native[1];
  }
  for (let index = turns.length - 1; index >= 0; index -= 1) {
    const turn = turns[index];
    if (turnMatchesCompletion(turn, completionId)) {
      return !turnHasCompletionFooter(turn);
    }
  }
  return true;
}

/** Whether an authoritative idle boundary still has a visible open tail.
 *
 * This is the terminal fallback for failed/interrupted turns, which do not
 * create an unread completion receipt. It deliberately inspects only the
 * newest row: older neutral steer/compaction segments may remain incomplete
 * while a later segment owns the native terminal.
 */
export function idleTurnNeedsHistoryRepair(
  turns: readonly CompletionRepairTurn[],
): boolean {
  const latest = turns[turns.length - 1];
  return !!latest && !turnHasCompletionFooter(latest);
}

export function catalogCompletionProjection(
  session: {
    completion_id?: string | null;
    completion_unread?: boolean | null;
    completion_revision?: number | null;
  } | null | undefined,
): CompletionProjection | null {
  if (session?.completion_unread == null
      || session.completion_revision == null) return null;
  return {
    id: session.completion_id ?? null,
    unread: session.completion_unread,
    revision: session.completion_revision,
  };
}

export function newestCompletionProjection(
  runtime: CompletionProjection | null | undefined,
  catalog: CompletionProjection | null | undefined,
): CompletionProjection | null {
  if (!runtime) return catalog ?? null;
  if (!catalog || runtime.revision >= catalog.revision) return runtime;
  return catalog;
}

export function markCompletionUnread(
  current: CompletionReceipts,
  parentSid: string,
  sourceSid: string,
  source: "main" | "btw",
  completionId: string | null = null,
  turnEndSeq: number | null = null,
  turnEndGeneration: string | null = null,
): CompletionReceipts {
  const prior = current[parentSid] ?? {
    main: false, mainCompletionId: null, mainTurnEndSeq: null,
    mainTurnEndGeneration: null, btwSids: [],
  };
  if (source === "main") {
    if (prior.main && prior.mainCompletionId === completionId
        && (completionId != null || (
          prior.mainTurnEndSeq === turnEndSeq
          && prior.mainTurnEndGeneration === turnEndGeneration
        ))) {
      return current;
    }
    return {
      ...current,
      [parentSid]: {
        ...prior,
        main: true,
        mainCompletionId: completionId,
        mainTurnEndSeq: turnEndSeq,
        mainTurnEndGeneration: turnEndGeneration,
      },
    };
  }
  if (prior.btwSids.includes(sourceSid)) return current;
  return {
    ...current,
    [parentSid]: {
      ...prior,
      btwSids: [...prior.btwSids, sourceSid],
    },
  };
}

export function acknowledgeCompletion(
  current: CompletionReceipts,
  parentSid: string,
  options: { main?: boolean; btwSid?: string },
): CompletionReceipts {
  const prior = current[parentSid];
  if (!prior) return current;
  const main = options.main ? false : prior.main;
  const mainCompletionId = options.main ? null : prior.mainCompletionId;
  const mainTurnEndSeq = options.main ? null : prior.mainTurnEndSeq;
  const mainTurnEndGeneration = options.main
    ? null : prior.mainTurnEndGeneration;
  const btwSids = options.btwSid
    ? prior.btwSids.filter((sid) => sid !== options.btwSid)
    : prior.btwSids;
  if (main === prior.main
      && mainCompletionId === prior.mainCompletionId
      && mainTurnEndSeq === prior.mainTurnEndSeq
      && mainTurnEndGeneration === prior.mainTurnEndGeneration
      && btwSids.length === prior.btwSids.length) {
    return current;
  }
  const next = { ...current };
  if (!main && btwSids.length === 0) delete next[parentSid];
  else next[parentSid] = {
    main, mainCompletionId, mainTurnEndSeq, mainTurnEndGeneration, btwSids,
  };
  return next;
}

/** Clear a local main fallback when the authoritative read receipt names the
 * same completion, or when an ordered clear follows its TurnEnd. A delayed
 * catalog/read frame for an older completion must not hide a newer turn_end
 * fallback which has not received its durable completion_state yet.
 */
export function acknowledgeMatchingCompletion(
  current: CompletionReceipts,
  parentSid: string,
  authoritative: CompletionProjection | null | undefined,
  options: {
    authoritativeSeq?: number | null;
    authoritativeGeneration?: string | null;
  } = {},
): CompletionReceipts {
  const local = current[parentSid];
  if (!local?.main || authoritative?.unread !== false) return current;
  const identityMatches = !!authoritative.id
    && local.mainCompletionId === authoritative.id;
  // A downstream CompletionState is ordered in the same per-session sequence
  // as TurnEnd. That causal boundary lets an authoritative generated id clear
  // a legacy/null-id fallback without letting an unordered catalog row do so.
  const causallyClearsFallback = (
    authoritative.id == null || local.mainCompletionId == null
  )
    && local.mainTurnEndSeq != null
    && local.mainTurnEndGeneration != null
    && options.authoritativeSeq != null
    && options.authoritativeGeneration === local.mainTurnEndGeneration
    && options.authoritativeSeq > local.mainTurnEndSeq;
  if (!identityMatches && !causallyClearsFallback) return current;
  return acknowledgeCompletion(current, parentSid, { main: true });
}

export function completionAcknowledgementId(
  receipt: CompletionReceipt | undefined,
  authoritative: { id: string | null; unread: boolean } | null | undefined,
): string | null {
  if (authoritative?.unread && authoritative.id) return authoritative.id;
  return receipt?.main ? receipt.mainCompletionId : null;
}

export function completionBadgeKind(
  receipt: CompletionReceipt | undefined,
  authoritativeUnread?: boolean | null,
): CompletionBadgeKind | undefined {
  const main = receipt?.main === true || authoritativeUnread === true;
  const btwSids = receipt?.btwSids ?? [];
  if (main && btwSids.length > 0) return "both";
  if (main) return "main";
  if (btwSids.length > 0) return "btw";
  return undefined;
}

export function rekeyCompletionReceipts(
  current: CompletionReceipts,
  oldSid: string,
  newSid: string,
): CompletionReceipts {
  if (oldSid === newSid || !current[oldSid]) return current;
  const source = current[oldSid];
  const target = current[newSid];
  const next = { ...current };
  delete next[oldSid];
  next[newSid] = target ? {
    main: target.main || source.main,
    mainCompletionId: source.main
      ? source.mainCompletionId : target.mainCompletionId,
    mainTurnEndSeq: source.main
      ? source.mainTurnEndSeq : target.mainTurnEndSeq,
    mainTurnEndGeneration: source.main
      ? source.mainTurnEndGeneration : target.mainTurnEndGeneration,
    btwSids: Array.from(new Set([...target.btwSids, ...source.btwSids])),
  } : source;
  return next;
}

export function discardBtwCompletionReceipts(
  current: CompletionReceipts,
): CompletionReceipts {
  let changed = false;
  const next: CompletionReceipts = {};
  for (const [parentSid, receipt] of Object.entries(current)) {
    if (receipt.btwSids.length > 0) changed = true;
    if (receipt.main) next[parentSid] = {
      main: true,
      mainCompletionId: receipt.mainCompletionId,
      mainTurnEndSeq: receipt.mainTurnEndSeq,
      mainTurnEndGeneration: receipt.mainTurnEndGeneration,
      btwSids: [],
    };
  }
  return changed ? next : current;
}
