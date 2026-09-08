import type { Turn } from "./domain/conversation";
import type { History } from "./protocol";

// Paint a small newest page first so one tool-heavy conversation cannot
// monopolize the socket and main thread before the current answer is usable.
export const HISTORY_INITIAL_PAGE = 4;
export const HISTORY_MORE_PAGE = 12;
export const HISTORY_PROVISIONAL_WATCHDOG_MS = 60_000;
export const HISTORY_LATEST_PAGE_KEY = "latest";

export function historyPageKey(before: string): string {
  return `before:${before}`;
}

/** Convert the protocol DTO into the browser's canonical summary projection. */
export function summaryHistoryTurns(history: History): Turn[] | null {
  if (history.detail !== "summary" || !Array.isArray(history.turns)) {
    return null;
  }
  return history.turns.map((turn) => ({
    ...turn,
    fileChangesTurnId: turn.fileChanges ? turn.id : undefined,
    blocks: turn.blocks as Turn["blocks"],
    clientMsgId: turn.clientMsgId ?? undefined,
    forkPointId: turn.forkPointId ?? undefined,
    checkpointId: turn.checkpointId ?? undefined,
    interrupted: turn.interrupted ?? undefined,
    error: turn.error ?? undefined,
    images: turn.images ?? undefined,
    imageRefs: turn.imageRefs ?? undefined,
    files: turn.files ?? undefined,
    ts: turn.ts ?? undefined,
    doneTs: turn.doneTs ?? undefined,
    durationMs: turn.durationMs ?? undefined,
    processDetailState: turn.processDetailState ?? undefined,
    detailReasons: turn.detailReasons
      ? [...turn.detailReasons] : undefined,
    processStartedTs: turn.processStartedTs ?? undefined,
    processDoneTs: turn.processDoneTs ?? undefined,
  }));
}
