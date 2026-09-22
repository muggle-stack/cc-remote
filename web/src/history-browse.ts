import {
  installAuthoritativeTurnDetailPage,
  mergeAuthoritativeTurnDetail,
  mergeHistoryPageCopies,
  reconcileProvenCompactionOrphans,
} from "./history-merge.ts";
import {
  MAX_RUNTIME_COMPLETED_UNITS,
  MAX_RUNTIME_TURNS,
} from "./runtime-bounds.ts";
import type {
  Turn,
  TurnDetailProjection,
} from "./domain/conversation.ts";

export interface HistoryBrowseLimits {
  maxTurns: number;
  maxBytes: number;
  lowWaterTurns: number;
  lowWaterBytes: number;
}

export const DEFAULT_HISTORY_BROWSE_LIMITS: HistoryBrowseLimits = {
  maxTurns: MAX_RUNTIME_TURNS,
  maxBytes: MAX_RUNTIME_COMPLETED_UNITS,
  lowWaterTurns: 1536,
  lowWaterBytes: 12 * 1024 * 1024,
};

/** One canonical summary page. `newerPageKey` is a local cache link, never a
 * wire cursor: protocol v21 can fetch older pages only. */
export interface HistoryBrowsePage {
  pageKey: string;
  turns: readonly Turn[];
  hasOlder?: boolean;
  olderCursor?: string | null;
  hasNewer?: boolean;
  newerPageKey?: string | null;
  isLatest?: boolean;
}

export interface HistoryBrowseSegment {
  pageKey: string;
  turns: Turn[];
  hasOlder: boolean;
  olderCursor: string | null;
  hasNewer: boolean;
  newerPageKey: string | null;
  isLatest: boolean;
}

/** Focused, display-only history state.
 *
 * The authoritative SessionRuntime remains the newest/live window. This object
 * is safe to discard on focus, revision, generation, or scope changes because
 * it never owns control, query acceptance, queue, or live lifecycle state. */
export interface HistoryBrowseProjection {
  scopeKey: string;
  sid: string;
  revision: string;
  generation: string | null;
  viewId: string;
  windowEpoch: number;
  turns: Turn[];
  segments: HistoryBrowseSegment[];
  loadedPageKeys: string[];
  oldestPageKey: string | null;
  newestPageKey: string | null;
  hasOlder: boolean;
  olderCursor: string | null;
  hasNewer: boolean;
  newerPageKey: string | null;
  latestDirty: boolean;
}

export interface CreateHistoryBrowseInput {
  scopeKey: string;
  sid: string;
  revision: string;
  generation?: string | null;
  /** Stable for this browse lifetime. App owns generation of this value. */
  viewId: string;
  baseTurns: readonly Turn[];
  basePageKey: string;
  hasOlder: boolean;
  olderCursor: string | null;
  olderPage?: HistoryBrowsePage;
  protectedTurnIds?: readonly string[];
  limits?: Partial<HistoryBrowseLimits>;
}

export interface HistoryBrowseMutationOptions {
  /** Frozen machine/surface scope captured when the read started. */
  expectedScopeKey?: string;
  expectedViewId?: string;
  expectedWindowEpoch?: number;
  expectedOlderCursor?: string | null;
  expectedNewerPageKey?: string | null;
  /** Canonical or display ids which bound eviction at the viewport anchor. */
  protectedTurnIds?: readonly string[];
  limits?: Partial<HistoryBrowseLimits>;
}

/** Pages removed from the display window must be written by App before relying
 * on `newerPageKey`. Returning them keeps the reducer pure and also covers the
 * synthetic base/latest segment, which did not originate in page IndexedDB. */
export interface HistoryBrowseMutation {
  projection: HistoryBrowseProjection;
  evictedPages: HistoryBrowsePage[];
}

export interface HistoryBrowseScopeGuard {
  expectedScopeKey?: string;
  expectedViewId?: string;
  expectedWindowEpoch?: number;
}

export interface HistoryBrowseCachedNewerGuard {
  sid: string;
  scopeKey: string;
  revision: string;
  generation: string | null;
  viewId: string;
  windowEpoch: number;
  pageKey: string;
}

/** Revalidate every authority field after an asynchronous page-cache read.
 * Dirty-state handling is deliberately separate: a still-current request must
 * be settled immediately when its cached latest page became stale. */
export function acceptsCachedNewerPage(
  projection: HistoryBrowseProjection,
  guard: HistoryBrowseCachedNewerGuard,
): boolean {
  return projection.sid === guard.sid
    && projection.scopeKey === guard.scopeKey
    && projection.revision === guard.revision
    && projection.generation === guard.generation
    && projection.viewId === guard.viewId
    && projection.windowEpoch === guard.windowEpoch
    && projection.hasNewer
    && projection.newerPageKey === guard.pageKey;
}

/** A cached latest page is only a navigation link after live output changed.
 * Installing its stale rows would hide the active tail. Reaching that link by
 * an explicit downward gesture must switch to SessionRuntime instead. */
export function cachedLatestRequiresLiveRuntime(
  projection: HistoryBrowseProjection,
  page: Pick<HistoryBrowsePage, "isLatest"> | null | undefined,
  latestPageKey?: string,
): boolean {
  return projection.latestDirty && (
    !!page?.isLatest
    || (!!latestPageKey && projection.newerPageKey === latestPageKey)
  );
}

export function canonicalTurnId(turn: Pick<Turn, "id" | "historyTurnId">): string {
  return turn.historyTurnId || turn.id;
}

function pageToSegment(page: HistoryBrowsePage): HistoryBrowseSegment {
  return {
    pageKey: page.pageKey,
    turns: [...page.turns],
    hasOlder: !!page.hasOlder,
    olderCursor: page.olderCursor ?? null,
    hasNewer: page.isLatest ? false : !!page.hasNewer || !!page.newerPageKey,
    newerPageKey: page.isLatest ? null : page.newerPageKey ?? null,
    isLatest: !!page.isLatest,
  };
}

function segmentToPage(segment: HistoryBrowseSegment): HistoryBrowsePage {
  return {
    pageKey: segment.pageKey,
    turns: [...segment.turns],
    hasOlder: segment.hasOlder,
    olderCursor: segment.olderCursor,
    hasNewer: segment.hasNewer,
    newerPageKey: segment.newerPageKey,
    isLatest: segment.isLatest,
  };
}

/** Newer segments win canonical overlap so an optimistic live id stays mounted
 * after its native history row materializes. */
function dedupeSegments(segments: readonly HistoryBrowseSegment[]): HistoryBrowseSegment[] {
  const copies = mergeHistoryPageCopies(flattenSegments(segments));
  let offset = 0;
  return segments.map((segment) => {
    const turns = copies.slice(offset, offset + segment.turns.length)
      .filter((turn): turn is Turn => turn != null);
    offset += segment.turns.length;
    return { ...segment, turns };
  });
}

function flattenSegments(segments: readonly HistoryBrowseSegment[]): Turn[] {
  const turns: Turn[] = [];
  for (const segment of segments) turns.push(...segment.turns);
  return turns;
}

/** Persist a canonical compaction repair in its owning page segment. Keeping
 * empty segments is intentional: page keys and cursors remain the authority
 * for walking back to an evicted neighbour even when the only polluted row in
 * one byte-window page was absorbed by the adjacent canonical page. */
function reconcileCompactionSegments(
  segments: readonly HistoryBrowseSegment[],
): HistoryBrowseSegment[] {
  const source = flattenSegments(segments);
  const repaired = reconcileProvenCompactionOrphans(source);
  if (repaired.length === source.length) return [...segments];
  // The repair is deletion-only and preserves the exact source objects.
  // Display ids are not unique across every cache migration: two legitimate
  // rows can share an optimistic id while carrying distinct historyTurnId
  // authorities. Filtering by object identity avoids replacing both with the
  // last Map entry for that display id.
  const retained = new Set(repaired);
  return segments.map((segment) => ({
    ...segment,
    turns: segment.turns.filter((turn) => retained.has(turn)),
  }));
}

function materializeProjection(
  projection: Omit<
    HistoryBrowseProjection,
    "turns" | "loadedPageKeys" | "oldestPageKey" | "newestPageKey"
  >,
): HistoryBrowseProjection {
  const segments = reconcileCompactionSegments(
    dedupeSegments(projection.segments));
  return {
    ...projection,
    segments,
    turns: flattenSegments(segments),
    loadedPageKeys: segments.map((segment) => segment.pageKey),
    oldestPageKey: segments[0]?.pageKey ?? null,
    newestPageKey: segments.at(-1)?.pageKey ?? null,
  };
}

function resolvedLimits(
  patch?: Partial<HistoryBrowseLimits>,
): HistoryBrowseLimits {
  const merged = { ...DEFAULT_HISTORY_BROWSE_LIMITS, ...patch };
  const maxTurns = Math.max(1, Math.floor(merged.maxTurns));
  const maxBytes = Math.max(1, Math.floor(merged.maxBytes));
  return {
    maxTurns,
    maxBytes,
    lowWaterTurns: Math.min(
      maxTurns,
      Math.max(1, Math.floor(merged.lowWaterTurns)),
    ),
    lowWaterBytes: Math.min(
      maxBytes,
      Math.max(1, Math.floor(merged.lowWaterBytes)),
    ),
  };
}

function encodedBytes(value: unknown, stopAfter: number): number {
  try {
    const encoded = JSON.stringify(value);
    if (encoded == null) return 0;
    if (encoded.length > stopAfter) return encoded.length;
    return new TextEncoder().encode(encoded).byteLength;
  } catch {
    return stopAfter + 1;
  }
}

function protectedIds(
  values?: readonly string[],
): ReadonlySet<string> {
  return new Set(values ?? []);
}

function turnIsProtected(turn: Turn, ids: ReadonlySet<string>): boolean {
  return ids.has(turn.id)
    || ids.has(canonicalTurnId(turn));
}

interface WindowStats {
  count: number;
  bytes: number;
}

function windowStats(
  segments: readonly HistoryBrowseSegment[],
  stopAfter: number,
): WindowStats {
  let count = 0;
  let bytes = 0;
  for (const segment of segments) {
    for (const turn of segment.turns) {
      count += 1;
      bytes += encodedBytes(turn, Math.max(0, stopAfter - bytes));
    }
  }
  return { count, bytes };
}

function overHighWater(stats: WindowStats, limits: HistoryBrowseLimits): boolean {
  return stats.count > limits.maxTurns || stats.bytes > limits.maxBytes;
}

function aboveLowWater(stats: WindowStats, limits: HistoryBrowseLimits): boolean {
  return stats.count > limits.lowWaterTurns || stats.bytes > limits.lowWaterBytes;
}

interface BoundedSegments {
  segments: HistoryBrowseSegment[];
  evictedPages: HistoryBrowsePage[];
  nearestEvictedPageKey: string | null;
  evicted: boolean;
}

/** Eviction is directional and contiguous. Only a caller-frozen viewport
 * anchor is protected: live/control and in-flight detail state remain
 * authoritative in SessionRuntime, so treating open browse rows as implicit
 * anchors would let every older page grow this display projection forever.
 * A protected row stops the scan instead of being skipped because evicting
 * around it would create an invisible hole and make the local page link
 * ambiguous. Explicit-anchor overflow is therefore intentional. */
function boundSegments(
  input: readonly HistoryBrowseSegment[],
  direction: "head" | "tail",
  ids: ReadonlySet<string>,
  limits: HistoryBrowseLimits,
): BoundedSegments {
  const segments = input.map((segment) => ({
    ...segment,
    turns: [...segment.turns],
  }));
  let stats = windowStats(segments, limits.maxBytes);
  if (!overHighWater(stats, limits)) {
    return {
      segments,
      evictedPages: [],
      nearestEvictedPageKey: null,
      evicted: false,
    };
  }

  const pageSnapshots = new Map<string, HistoryBrowsePage>();
  let nearestEvictedPageKey: string | null = null;
  let blocked = false;

  while (aboveLowWater(stats, limits) && !blocked) {
    let segmentIndex = direction === "head" ? 0 : segments.length - 1;
    while (segmentIndex >= 0 && segmentIndex < segments.length
        && segments[segmentIndex].turns.length === 0) {
      segmentIndex += direction === "head" ? 1 : -1;
    }
    if (segmentIndex < 0 || segmentIndex >= segments.length) break;

    const segment = segments[segmentIndex];
    const turnIndex = direction === "head" ? 0 : segment.turns.length - 1;
    const turn = segment.turns[turnIndex];
    if (!turn || turnIsProtected(turn, ids)) {
      blocked = true;
      break;
    }

    if (!pageSnapshots.has(segment.pageKey)) {
      pageSnapshots.set(segment.pageKey, segmentToPage(segment));
    }
    nearestEvictedPageKey = segment.pageKey;
    segment.turns.splice(turnIndex, 1);
    stats = {
      count: Math.max(0, stats.count - 1),
      bytes: Math.max(0, stats.bytes - encodedBytes(turn, limits.maxBytes)),
    };
    if (segment.turns.length === 0) segments.splice(segmentIndex, 1);
  }

  return {
    segments,
    evictedPages: [...pageSnapshots.values()],
    nearestEvictedPageKey,
    evicted: pageSnapshots.size > 0,
  };
}

function guardMatches(
  projection: HistoryBrowseProjection,
  guard: HistoryBrowseScopeGuard,
): boolean {
  return (guard.expectedScopeKey == null
      || projection.scopeKey === guard.expectedScopeKey)
    && (guard.expectedViewId == null
      || projection.viewId === guard.expectedViewId)
    && (guard.expectedWindowEpoch == null
      || projection.windowEpoch === guard.expectedWindowEpoch);
}

function unchanged(projection: HistoryBrowseProjection): HistoryBrowseMutation {
  return { projection, evictedPages: [] };
}

export function createHistoryBrowse(
  input: CreateHistoryBrowseInput,
): HistoryBrowseMutation {
  const basePage: HistoryBrowsePage = {
    pageKey: input.basePageKey,
    turns: input.baseTurns,
    hasOlder: input.hasOlder,
    olderCursor: input.olderCursor,
    hasNewer: false,
    newerPageKey: null,
    isLatest: true,
  };
  const projection = materializeProjection({
    scopeKey: input.scopeKey,
    sid: input.sid,
    revision: input.revision,
    generation: input.generation ?? null,
    viewId: input.viewId,
    windowEpoch: 0,
    segments: [pageToSegment(basePage)],
    hasOlder: input.hasOlder,
    olderCursor: input.olderCursor,
    hasNewer: false,
    newerPageKey: null,
    latestDirty: false,
  });
  if (!input.olderPage) return { projection, evictedPages: [] };
  return prependOlderPage(projection, input.olderPage, {
    expectedScopeKey: input.scopeKey,
    protectedTurnIds: input.protectedTurnIds,
    limits: input.limits,
  });
}

/** Install a server `before` page without mutating the authoritative runtime. */
export function prependOlderPage(
  projection: HistoryBrowseProjection,
  page: HistoryBrowsePage,
  options: HistoryBrowseMutationOptions = {},
): HistoryBrowseMutation {
  if (!guardMatches(projection, options)
      || (options.expectedOlderCursor !== undefined
        && projection.olderCursor !== options.expectedOlderCursor)) {
    return unchanged(projection);
  }
  const incoming = pageToSegment(page);
  const withoutSamePage = projection.segments.filter(
    (segment) => segment.pageKey !== incoming.pageKey,
  );
  const normalized = reconcileCompactionSegments(
    dedupeSegments([incoming, ...withoutSamePage]));
  const bounded = boundSegments(
    normalized,
    "tail",
    protectedIds(options.protectedTurnIds),
    resolvedLimits(options.limits),
  );
  const next = materializeProjection({
    ...projection,
    windowEpoch: projection.windowEpoch + 1,
    segments: bounded.segments,
    hasOlder: incoming.hasOlder,
    olderCursor: incoming.olderCursor
      ?? (normalized[0]?.turns[0]
        ? canonicalTurnId(normalized[0].turns[0])
        : null),
    hasNewer: bounded.evicted || projection.hasNewer,
    newerPageKey: bounded.evicted
      ? bounded.nearestEvictedPageKey
      : projection.newerPageKey,
  });
  return { projection: next, evictedPages: bounded.evictedPages };
}

/** Install one already-cached newer page. Protocol v21 has no server `after`,
 * so a cache miss must be handled by App's explicit return-to-latest action. */
export function appendNewerPage(
  projection: HistoryBrowseProjection,
  page: HistoryBrowsePage,
  options: HistoryBrowseMutationOptions = {},
): HistoryBrowseMutation {
  if (!guardMatches(projection, options)
      || (options.expectedNewerPageKey !== undefined
        && projection.newerPageKey !== options.expectedNewerPageKey)) {
    return unchanged(projection);
  }
  const incoming = pageToSegment(page);
  const withoutSamePage = projection.segments.filter(
    (segment) => segment.pageKey !== incoming.pageKey,
  );
  const normalized = reconcileCompactionSegments(
    dedupeSegments([...withoutSamePage, incoming]));
  const bounded = boundSegments(
    normalized,
    "head",
    protectedIds(options.protectedTurnIds),
    resolvedLimits(options.limits),
  );
  const visible = flattenSegments(bounded.segments);
  const firstRetained = visible[0];
  const pageHasNewer = incoming.isLatest
    ? false
    : incoming.hasNewer || !!incoming.newerPageKey;
  const next = materializeProjection({
    ...projection,
    windowEpoch: projection.windowEpoch + 1,
    segments: bounded.segments,
    hasOlder: bounded.evicted || projection.hasOlder,
    olderCursor: bounded.evicted
      ? (firstRetained ? canonicalTurnId(firstRetained) : projection.olderCursor)
      : projection.olderCursor,
    hasNewer: pageHasNewer,
    newerPageKey: pageHasNewer ? incoming.newerPageKey : null,
  });
  return { projection: next, evictedPages: bounded.evictedPages };
}

export function markBrowseDetail(
  projection: HistoryBrowseProjection,
  turnId: string,
  detail: Turn,
  page?: {
    hasMore: boolean;
    oldestCursor?: string | null;
    hasNewer: boolean;
    newerCursor?: string | null;
  },
  guard: HistoryBrowseScopeGuard = {},
  detailProjection?: TurnDetailProjection,
): HistoryBrowseProjection {
  if (!guardMatches(projection, guard)) return projection;
  let changed = false;
  const segments = projection.segments.map((segment) => ({
    ...segment,
    turns: segment.turns.map((turn) => {
      if (turn.id !== turnId && canonicalTurnId(turn) !== turnId) return turn;
      changed = true;
      return page
        ? installAuthoritativeTurnDetailPage(
          turn, detail, page, detailProjection)
        : mergeAuthoritativeTurnDetail(turn, detail);
    }),
  }));
  if (!changed) return projection;
  return materializeProjection({
    ...projection,
    segments,
  });
}

export function markBrowseDetailLoading(
  projection: HistoryBrowseProjection,
  turnId: string,
  loading: boolean,
  guard: HistoryBrowseScopeGuard = {},
  autoLoad?: boolean,
  error?: string | null,
  retry?: {
    before: string | null;
    direction: "initial" | "older" | "newer";
  } | null,
  resetPending?: boolean,
): HistoryBrowseProjection {
  if (!guardMatches(projection, guard)) return projection;
  let changed = false;
  const segments = projection.segments.map((segment) => ({
    ...segment,
    turns: segment.turns.map((turn) => {
      if (turn.id !== turnId && canonicalTurnId(turn) !== turnId) return turn;
      if (!!turn.detailLoading === loading
          && (autoLoad === undefined
            || !!turn.detailAutoLoad === autoLoad)
          && (error === undefined
            || turn.detailError === (error ?? undefined))
          && (retry === undefined
            || (turn.detailRetryBefore === (
              retry === null ? undefined : retry.before)
              && turn.detailRetryDirection === (
                retry === null ? undefined : retry.direction)))
          && (resetPending === undefined
            || !!turn.detailResetPending === resetPending)) return turn;
      changed = true;
      return {
        ...turn,
        detailLoading: loading,
        detailAutoLoad: autoLoad ?? turn.detailAutoLoad,
        detailError: error === undefined
          ? turn.detailError : error ?? undefined,
        detailRetryBefore: retry === undefined
          ? turn.detailRetryBefore : retry?.before,
        detailRetryDirection: retry === undefined
          ? turn.detailRetryDirection : retry?.direction,
        detailResetPending:
          resetPending ?? turn.detailResetPending,
      };
    }),
  }));
  return changed ? materializeProjection({ ...projection, segments }) : projection;
}

/** A protocol-v21 browser can fetch newer pages only from its local page
 * cache. A cache miss keeps the readable window mounted and changes the
 * downward affordance to the always-safe "return to latest" action. */
export function markBrowseNewerUnavailable(
  projection: HistoryBrowseProjection,
  guard: HistoryBrowseScopeGuard = {},
): HistoryBrowseProjection {
  if (!guardMatches(projection, guard)
      || (!projection.hasNewer && projection.newerPageKey == null)) {
    return projection;
  }
  return {
    ...projection,
    hasNewer: false,
    newerPageKey: null,
  };
}

/** Release ChatView's keyed page transaction after the bounded retry has also
 * failed. The cursor stays available for a later explicit gesture. */
export function settleBrowsePageRequest(
  projection: HistoryBrowseProjection,
  guard: HistoryBrowseScopeGuard = {},
): HistoryBrowseProjection {
  if (!guardMatches(projection, guard)) return projection;
  return {
    ...projection,
    windowEpoch: projection.windowEpoch + 1,
  };
}

export function markBrowseLatestDirty(
  projection: HistoryBrowseProjection,
  guard: HistoryBrowseScopeGuard = {},
): HistoryBrowseProjection {
  if (!guardMatches(projection, guard)
      || projection.latestDirty) return projection;
  return { ...projection, latestDirty: true };
}
