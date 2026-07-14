import type { SessionInfo, State } from "./protocol";

export const WORKTREE_FORK_NAME_MAX = 80;

export interface SessionMenuCapabilities {
  rename: boolean;
  archive: boolean;
  forkWorktree: boolean;
}

export interface PendingWorktreeFork {
  requestId: string;
  parentSessionId: string;
}

export interface PendingSessionFork extends PendingWorktreeFork {
  forkPointId: string;
  engine: "claude" | "codex";
}

export function sessionMenuCapabilities(session: SessionInfo): SessionMenuCapabilities {
  return {
    rename: true,
    archive: true,
    forkWorktree: session.engine === "codex" && session.tag !== "archived",
  };
}

export function isWorktreeForkBlockedByState(state?: State | null): boolean {
  return state === "running" || state === "interrupting";
}

export function normalizeWorktreeForkName(value: string): string {
  return value.trim();
}

export function isWorktreeForkNameValid(value: string): boolean {
  const normalized = normalizeWorktreeForkName(value);
  return normalized.length > 0 && normalized.length <= WORKTREE_FORK_NAME_MAX;
}

export function matchesWorktreeForkRequest(
  pending: PendingWorktreeFork | null,
  requestId: string | null | undefined,
  parentSessionId?: string | null,
): boolean {
  return pending !== null
    && requestId === pending.requestId
    && (parentSessionId == null || parentSessionId === pending.parentSessionId);
}

export function matchesSessionForkRequest(
  pending: PendingSessionFork | null,
  requestId: string | null | undefined,
  parentSessionId?: string | null,
  forkPointId?: string | null,
): boolean {
  return matchesWorktreeForkRequest(pending, requestId, parentSessionId)
    && (forkPointId == null || forkPointId === pending!.forkPointId);
}

export function canForkTurn<T extends { done: boolean; forkPointId?: string }>(
  engine: "claude" | "codex",
  turn: T,
): turn is T & { done: true; forkPointId: string } {
  return (engine === "claude" || engine === "codex")
    && turn.done && !!turn.forkPointId;
}

/** Provisional errors keep the reliable command and its UI ownership pending.
 * `fork_reconciling` means app-server may have committed the mutation, so the
 * same request id is being reconciled and the user must not launch a second. */
export function isTerminalWorktreeForkError(code: string): boolean {
  return code !== "wrapper_offline" && code !== "fork_reconciling";
}
