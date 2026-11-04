/** The first pending input remains the session's acceptance barrier. Claude
 * can append more steers, each of which must keep its own exact receipt. */
export const MAX_PENDING_STEERS = 32;
export type SteerAcceptanceKind = "steer" | "steer_unknown";

export interface PendingSteerState {
  acceptancePending: string | null;
  acceptanceKind: "query" | SteerAcceptanceKind | null;
  additionalSteerAcceptances?: Record<string, SteerAcceptanceKind>;
}

export function pendingAcceptanceIds(state: PendingSteerState): string[] {
  return [
    ...(state.acceptancePending ? [state.acceptancePending] : []),
    ...Object.keys(state.additionalSteerAcceptances ?? {}),
  ];
}

export function acceptanceKindFor(state: PendingSteerState, id: string) {
  return id === state.acceptancePending
    ? state.acceptanceKind : state.additionalSteerAcceptances?.[id] ?? null;
}

export function canSubmitSteer(state: PendingSteerState, engine?: string): boolean {
  const ids = pendingAcceptanceIds(state);
  return ids.length === 0 || (engine === "claude"
    && ids.length < MAX_PENDING_STEERS
    && ids.every(id => acceptanceKindFor(state, id) === "steer"));
}
