import type { History } from "./protocol";
import type { Turn } from "./domain/conversation";
import type { HistoryBrowseProjection } from "./history-browse";

/**
 * History recovery only needs this projection of a session runtime. Keeping
 * the feature boundary structural avoids making the history layer depend on
 * the root reducer that consumes it.
 */
export interface HistoryRuntimeState {
  turns: Turn[];
  hasMore?: boolean;
  oldestId?: string | null;
  historyInvalidated: boolean;
  historyRevision: string | null;
  historyContinuityRevision?: string | null;
  historyGeneration: string | null;
  historyBuildSeq: number;
  pendingHistoryRevision: string | null;
  pendingHistoryGeneration: string | null;
  pendingHistoryCandidateBuildSeq: number | null;
}

/**
 * The last painted history while a truncated/rebuild replay or sampled
 * append-prefix preview reconstructs the real runtime in parallel. Only one
 * projection lives at AppState level, so a long reconnect cannot duplicate
 * every resident session's bounded history.
 *
 * `turns === null` means the matching History has committed. The small
 * committed shell keeps the old scroll scope stable until a genuinely newer
 * revision replaces it; it no longer retains transcript data.
 */
export interface HistoryRecoveryProjection {
  sid: string;
  turns: Turn[] | null;
  /** Exact browser-owned row which may keep rendering live activity while the
   * old readable projection is frozen. Never infer this from row order. */
  activeOwnerId: string | null;
  hasMore: boolean;
  oldestId: string | null;
  viewRevision: string | null;
  expectedGeneration: string | null;
  candidateBuildSeq: number | null;
  acceptedRevision: string | null;
}

export function isHistoryRecoveryPending(
  recovery: HistoryRecoveryProjection | null | undefined,
  sid?: string | null,
): boolean {
  return !!recovery && recovery.turns !== null
    && (sid == null || recovery.sid === sid);
}

export function beginHistoryRecovery(
  current: HistoryRecoveryProjection | null,
  sid: string,
  runtime: HistoryRuntimeState,
  generation?: string | null,
  candidateBuildSeq: number | null = null,
  activeOwnerId?: string | null,
): HistoryRecoveryProjection {
  const retained = current?.sid === sid && current.turns !== null
    ? current
    : null;
  return {
    sid,
    // Move the already-bounded array into display-only state. The runtime gets
    // a fresh empty array before any replay frame can mutate it.
    turns: retained?.turns ?? runtime.turns,
    activeOwnerId: activeOwnerId !== undefined
      ? activeOwnerId : retained?.activeOwnerId ?? null,
    hasMore: retained?.hasMore ?? !!runtime.hasMore,
    oldestId: retained?.oldestId ?? runtime.oldestId ?? null,
    viewRevision: retained?.viewRevision
      ?? (current?.sid === sid ? current.viewRevision
        : runtime.historyContinuityRevision ?? runtime.historyRevision),
    expectedGeneration: generation ?? retained?.expectedGeneration
      ?? runtime.pendingHistoryGeneration ?? null,
    // Replay gaps start at null because a History build may already be in
    // flight: the first matching response becomes the candidate and explicitly
    // triggers one newer build. A sampled append-prefix already carries the
    // candidate build sequence, so its scheduled exact refresh may commit
    // directly without causing another potentially multi-GB transcript scan.
    candidateBuildSeq,
    acceptedRevision: null,
  };
}

/** A recovery page is usable only when it is an authoritative first page from
 * the wrapper generation that opened the replay envelope. Rollback revision
 * matching remains the reducer's separate pendingHistoryRevision barrier. */
export function historyMatchesRecovery(
  recovery: HistoryRecoveryProjection | null | undefined,
  history: Pick<
    History,
    "session_id" | "generation" | "before" | "authoritative"
  >,
): boolean {
  if (!isHistoryRecoveryPending(recovery)
      || recovery!.sid !== history.session_id
      || history.before
      || history.authoritative === false
      || history.generation == null) return false;
  return recovery!.expectedGeneration == null
    || history.generation === recovery!.expectedGeneration;
}

export function advanceHistoryRecovery(
  recovery: HistoryRecoveryProjection | null,
  history: Pick<
    History,
    "session_id" | "revision" | "generation" | "build_seq"
      | "before" | "authoritative"
  >,
): HistoryRecoveryProjection | null {
  if (!recovery || recovery.sid !== history.session_id
      || history.before || history.authoritative === false) return recovery;
  if (recovery.turns !== null) {
    if (!historyMatchesRecovery(recovery, history)) return recovery;
    const buildSeq = history.build_seq ?? 0;
    if (recovery.candidateBuildSeq == null) {
      return {
        ...recovery,
        expectedGeneration: history.generation ?? recovery.expectedGeneration,
        candidateBuildSeq: buildSeq,
      };
    }
    if (buildSeq <= recovery.candidateBuildSeq) return recovery;
    return {
      ...recovery,
      turns: null,
      hasMore: false,
      oldestId: null,
      candidateBuildSeq: null,
      acceptedRevision: history.revision,
    };
  }
  // Same-revision refreshes keep the view key stable. A later independent
  // revision replacement regains the normal ChatView reset behavior.
  return history.revision === recovery.acceptedRevision ? recovery : null;
}

type RuntimeHistoryRecoveryState = Pick<
  HistoryRuntimeState,
  "historyInvalidated" | "historyRevision" | "historyGeneration"
    | "historyBuildSeq" | "pendingHistoryRevision"
    | "pendingHistoryGeneration" | "pendingHistoryCandidateBuildSeq"
>;

/** Replay-gap confirmation is durable per runtime; the global projection only
 * owns the currently visible copy of old turns and may disappear on focus. */
export function isRuntimeHistoryRecoveryPending(
  runtime: RuntimeHistoryRecoveryState | null | undefined,
): boolean {
  return !!runtime?.historyInvalidated
    && runtime.pendingHistoryRevision == null;
}

export function historyMatchesRuntimeRecovery(
  runtime: RuntimeHistoryRecoveryState | null | undefined,
  history: Pick<
    History,
    "session_id" | "generation" | "build_seq" | "before" | "authoritative"
  >,
): boolean {
  if (!isRuntimeHistoryRecoveryPending(runtime)
      || history.before
      || history.authoritative === false
      || history.generation == null
      || (runtime!.pendingHistoryGeneration != null
        && history.generation !== runtime!.pendingHistoryGeneration)) {
    return false;
  }
  const sameAcceptedGeneration =
    runtime!.historyGeneration === history.generation;
  return !sameAcceptedGeneration
    || (history.build_seq ?? 0) >= runtime!.historyBuildSeq;
}

/** The first authoritative page after a replay gap is only a candidate.
 *
 * App must complete the coordinator entry for that response and then issue one
 * explicit newest-page request bound to the revealed generation. Prefix-preview
 * recovery does not mark its canonical runtime invalid, so it never enters this
 * expensive two-read path.
 */
export function historyNeedsConfirmationRequest(
  runtime: RuntimeHistoryRecoveryState | null | undefined,
  history: Pick<
    History,
    "session_id" | "generation" | "build_seq" | "before" | "authoritative"
  >,
): boolean {
  return historyMatchesRuntimeRecovery(runtime, history)
    && runtime!.pendingHistoryCandidateBuildSeq == null;
}

export function historyConfirmsRuntimeRecovery(
  runtime: RuntimeHistoryRecoveryState | null | undefined,
  history: Pick<
    History,
    "session_id" | "generation" | "build_seq" | "before" | "authoritative"
  >,
): boolean {
  return historyMatchesRuntimeRecovery(runtime, history)
    && runtime!.pendingHistoryCandidateBuildSeq != null
    && (history.build_seq ?? 0) > runtime!.pendingHistoryCandidateBuildSeq;
}

export function historyConfirmsRecovery(
  recovery: HistoryRecoveryProjection | null | undefined,
  history: Pick<
    History,
    "session_id" | "generation" | "build_seq" | "before" | "authoritative"
  >,
): boolean {
  return historyMatchesRecovery(recovery, history)
    && recovery!.candidateBuildSeq != null
    && (history.build_seq ?? 0) > recovery!.candidateBuildSeq;
}

export interface DisplayHistoryProjection {
  turns: Turn[];
  activeOwnerId: string | null;
  hasMore: boolean;
  /** The visible cursor belongs to the currently verified wrapper projection. */
  pagingReady: boolean;
  oldestId: string | null;
  viewRevision: string | null;
  generation: string | null;
  recovering: boolean;
  browsing: boolean;
  scopeKey: string | null;
  viewId: string | null;
  windowEpoch: number;
  hasNewer: boolean;
  newerPageKey: string | null;
  latestDirty: boolean;
}

export function displayHistoryProjection(
  recovery: HistoryRecoveryProjection | null | undefined,
  sid: string | null,
  runtime: HistoryRuntimeState,
  browse?: HistoryBrowseProjection | null,
  retainedBrowse?: HistoryBrowseProjection | null,
): DisplayHistoryProjection {
  if (sid && retainedBrowse?.sid === sid) {
    return {
      turns: retainedBrowse.turns,
      activeOwnerId: null,
      // Preserve the truthful affordance while making its stale cursor
      // inoperable. A same-revision/generation first page reactivates it.
      hasMore: retainedBrowse.hasOlder,
      pagingReady: false,
      oldestId: retainedBrowse.olderCursor,
      viewRevision: runtime.historyContinuityRevision ?? retainedBrowse.revision,
      generation: retainedBrowse.generation,
      recovering: true,
      browsing: true,
      scopeKey: retainedBrowse.scopeKey,
      viewId: retainedBrowse.viewId,
      windowEpoch: retainedBrowse.windowEpoch,
      hasNewer: false,
      newerPageKey: null,
      latestDirty: retainedBrowse.latestDirty,
    };
  }
  if (sid && isHistoryRecoveryPending(recovery, sid)) {
    return {
      turns: recovery!.turns!,
      activeOwnerId: recovery!.activeOwnerId,
      // The retained cursor belongs to the old generation. Keep the old rows
      // readable, but never issue pagination/detail reads from display-only
      // state while the authoritative projection is rebuilding.
      hasMore: recovery!.hasMore,
      pagingReady: false,
      oldestId: recovery!.oldestId,
      viewRevision: recovery!.viewRevision,
      generation: recovery!.expectedGeneration,
      recovering: true,
      browsing: false,
      scopeKey: null,
      viewId: null,
      windowEpoch: 0,
      hasNewer: false,
      newerPageKey: null,
      latestDirty: false,
    };
  }
  if (sid && browse?.sid === sid
      && browse.revision === runtime.historyRevision
      && (browse.generation == null
        || browse.generation === runtime.historyGeneration)) {
    return {
      turns: browse.turns,
      activeOwnerId: null,
      hasMore: browse.hasOlder,
      pagingReady: true,
      oldestId: browse.olderCursor,
      viewRevision: runtime.historyContinuityRevision ?? browse.revision,
      generation: browse.generation,
      recovering: false,
      browsing: true,
      scopeKey: browse.scopeKey,
      viewId: browse.viewId,
      windowEpoch: browse.windowEpoch,
      hasNewer: browse.hasNewer,
      newerPageKey: browse.newerPageKey,
      latestDirty: browse.latestDirty,
    };
  }
  const committed = sid && recovery?.sid === sid && recovery.turns === null
    ? recovery : null;
  return {
    turns: runtime.turns,
    activeOwnerId: null,
    hasMore: !!runtime.hasMore,
    pagingReady: !runtime.historyInvalidated,
    oldestId: runtime.oldestId ?? null,
    viewRevision: committed?.viewRevision ?? runtime.historyContinuityRevision ?? runtime.historyRevision,
    generation: runtime.historyGeneration,
    recovering: false,
    browsing: false,
    scopeKey: null,
    viewId: null,
    windowEpoch: 0,
    hasNewer: false,
    newerPageKey: null,
    latestDirty: false,
  };
}
