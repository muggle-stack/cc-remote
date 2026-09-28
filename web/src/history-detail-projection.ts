import type { ServerEvent } from "./protocol";
import type {
  Block,
  Turn,
  TurnDetailProjection,
  TurnDetailSegment,
} from "./domain/conversation";
export type {
  TurnDetailProjection,
  TurnDetailSegment,
} from "./domain/conversation";

export const MAX_DETAIL_PROJECTION_ITEMS = 4_000;
export const MAX_DETAIL_PROJECTION_CHARS = 32 * 1024 * 1024;

const LATEST_DETAIL_PAGE_KEY = "__cc_remote_detail_latest__";

export interface TurnDetailProjectionPage {
  before?: string | null;
  events: ServerEvent[];
  hasMore?: boolean;
  oldestCursor?: string | null;
  hasNewer?: boolean;
  newerCursor?: string | null;
}

export interface InstalledTurnDetailProjection {
  projection: TurnDetailProjection;
  detail: Turn | undefined;
}

export function nextAutoLoadDetailTurn(
  turns: readonly Turn[],
): { turnId: string; before: string } | null {
  const turn = turns.find((candidate) =>
    candidate.detailAutoLoad === true
    && candidate.detailLoading !== true
    && candidate.detailHasMore === true
    && !!candidate.detailOldestCursor);
  return turn?.detailOldestCursor
    ? { turnId: turn.id, before: turn.detailOldestCursor }
    : null;
}

function hasTurnIdentity(
  turn: Pick<Turn, "id" | "clientMsgId" | "historyTurnId">,
  identity: string,
): boolean {
  return [turn.id, turn.clientMsgId, turn.historyTurnId].includes(identity);
}

/** Select the next detail request for the one canonical active History head.
 *
 * Recency, array position, prompt text, and native task ids are deliberately
 * insufficient: a reconnect may retain several unfinished local rows, while
 * ``historyNewestId`` is the wrapper's exact source projection. Recovery loads
 * only the bounded newest page. Once that canonical page is installed, older
 * pages remain available through the existing explicit pagination controls;
 * reconnect must not turn one long active turn into an unbounded DOM restore. */
export function nextActiveDetailRequest(
  turns: readonly Turn[],
  historyNewestId: string | null | undefined,
  active: boolean,
): { turnId: string; before?: string } | null {
  if (!active || !historyNewestId) return null;
  const matches = turns.filter((turn) =>
    !turn.done && hasTurnIdentity(turn, historyNewestId));
  if (matches.length !== 1) return null;
  const turn = matches[0];
  if (turn.detailLoading === true
      || turn.detailRestorePending === true
      || turn.detailResetPending === true
      || !!turn.detailError
      || turn.detailProjection?.capped === true) return null;

  const projection = turn.detailProjection;
  if (projection && projection.segments.length > 0) {
    return null;
  }
  return turn.detailLoaded === true ? null : { turnId: turn.id };
}

function pageKey(before: string | null | undefined): string {
  return before ?? LATEST_DETAIL_PAGE_KEY;
}

function encodedChars(value: unknown): number {
  try {
    return JSON.stringify(value)?.length ?? 0;
  } catch {
    return Number.MAX_SAFE_INTEGER;
  }
}

function isFinal(block: Block): boolean {
  return block.kind === "text" && block.channel === "final";
}

function visibleBlocks(detail: Turn | undefined): Block[] {
  return detail?.blocks.filter((block) => !isFinal(block)) ?? [];
}

function insertSegment(
  current: readonly TurnDetailSegment[],
  incoming: TurnDetailSegment,
): TurnDetailSegment[] {
  const withoutSamePage = current.filter(
    (segment) => segment.pageKey !== incoming.pageKey);
  if (incoming.before === null) return [...withoutSamePage, incoming];

  // An older request uses the oldest cursor of the page already on screen.
  // Insert directly before that anchor; cursor values are opaque and must
  // never be sorted lexically or numerically by the browser.
  const olderAnchor = withoutSamePage.findIndex(
    (segment) => segment.oldestCursor === incoming.before);
  if (olderAnchor >= 0) {
    return [
      ...withoutSamePage.slice(0, olderAnchor),
      incoming,
      ...withoutSamePage.slice(olderAnchor),
    ];
  }

  // This path supports a locally-triggered newer read without making it the
  // primary product interaction. It is also useful when a retry fills a hole.
  const newerAnchor = withoutSamePage.findIndex(
    (segment) => segment.newerCursor === incoming.before);
  if (newerAnchor >= 0) {
    return [
      ...withoutSamePage.slice(0, newerAnchor + 1),
      incoming,
      ...withoutSamePage.slice(newerAnchor + 1),
    ];
  }

  // A response whose linkage cannot be proven should not reorder already
  // accepted pages. `hasNewer` is authoritative evidence that it belongs on
  // the older side; otherwise keep it at the newest edge.
  return incoming.hasNewer
    ? [incoming, ...withoutSamePage]
    : [...withoutSamePage, incoming];
}

function blockIdentity(block: Block): string {
  if (block.kind === "text") return `text:${block.message_id}`;
  if (block.kind === "tool") return `tool:${block.tool_use_id}`;
  return `process:${block.item_id}`;
}

function materializeSegments(
  segments: readonly TurnDetailSegment[],
  decode: (events: ServerEvent[]) => Turn | undefined,
): { detail: Turn | undefined; blocks: Block[] } {
  let detail: Turn | undefined;
  const blocks: Block[] = [];
  const blockIndexes = new Map<string, number>();

  // A wire page is deliberately a self-contained turn: the wrapper repeats
  // the user/terminal envelope around a source-disjoint set of display groups.
  // Decoding concatenated raw pages makes an intermediate TurnEnd close the
  // target before the next page and silently projects later commentary/tools
  // into a prompt-less turn. Decode each page independently, then join only
  // its authoritative display-block identities in cursor order.
  for (const segment of segments) {
    const decoded = decode(segment.events);
    if (!decoded) continue;
    detail = decoded;
    for (const block of visibleBlocks(decoded)) {
      const identity = blockIdentity(block);
      const existing = blockIndexes.get(identity);
      if (existing == null) {
        blockIndexes.set(identity, blocks.length);
        blocks.push(block);
      } else {
        // Retry/repair windows may overlap at an exact native block boundary.
        // The later source page refreshes that payload in place; equal text or
        // titles are never treated as identity evidence.
        const previous = blocks[existing];
        if (previous.kind === "tool" && block.kind === "tool"
            && block.tool === "tool" && Object.keys(block.input).length === 0) {
          // A rollout byte window can begin with an output whose call is on
          // the preceding page. Keep the recovered call metadata and take the
          // newer result; the generic output-only envelope is not a new tool.
          blocks[existing] = { ...previous, ...block,
            tool: previous.tool, input: previous.input, category: previous.category,
            title: previous.title, parent_id: previous.parent_id, server: previous.server,
            startedTs: previous.startedTs ?? block.startedTs };
        } else {
          blocks[existing] = block;
        }
      }
    }
  }
  return { detail, blocks };
}

function segmentChars(segments: readonly TurnDetailSegment[]): number {
  return segments.reduce((sum, segment) => sum + segment.encodedChars, 0);
}

/** Add or replace one cursor-keyed detail page and materialize all retained
 * source events together.
 *
 * The decode callback deliberately belongs to reducer.ts, which already owns
 * the protocol-event-to-Block state machine. Keeping raw ordered segments here
 * avoids a second, subtly incompatible event translator.
 */
export function installTurnDetailProjectionPage(
  current: TurnDetailProjection | undefined,
  page: TurnDetailProjectionPage,
  decode: (events: ServerEvent[]) => Turn | undefined,
  limits: {
    maxItems?: number;
    maxChars?: number;
  } = {},
): InstalledTurnDetailProjection {
  const maxItems = Math.max(
    1, Math.floor(limits.maxItems ?? MAX_DETAIL_PROJECTION_ITEMS));
  const maxChars = Math.max(
    1, Math.floor(limits.maxChars ?? MAX_DETAIL_PROJECTION_CHARS));
  const before = page.before ?? null;
  const incoming: TurnDetailSegment = {
    pageKey: pageKey(before),
    before,
    events: page.events.map((event) => ({ ...event })),
    hasMore: !!page.hasMore,
    oldestCursor: page.oldestCursor ?? null,
    hasNewer: !!page.hasNewer,
    newerCursor: page.newerCursor ?? null,
    encodedChars: encodedChars(page.events),
  };
  const navigation = before === null
    ? "latest"
    : current?.oldestCursor === before
      ? "older"
      : current?.newerCursor === before ? "newer" : "unknown";

  let segments = insertSegment(current?.segments ?? [], incoming);
  let materialized = materializeSegments(segments, decode);
  let detail = materialized.detail;
  let blocks = materialized.blocks;
  let capped = current?.capped ?? false;

  // Pages are bounded to 256 events/8 MiB by the wrapper. Keep a sliding
  // in-memory window and drop complete segments from the side opposite the
  // reader's request. The adjacent source cursor remains available in both
  // directions, so a memory limit is never presented as a history limit.
  const retainOlderSide = navigation === "older"
    || (navigation === "unknown" && incoming.hasNewer);
  while (segments.length > 1 && (
    blocks.length > maxItems
    || encodedChars(blocks) > maxChars
    || segmentChars(segments) > maxChars
  )) {
    segments = retainOlderSide
      ? segments.slice(0, -1) : segments.slice(1);
    capped = true;
    materialized = materializeSegments(segments, decode);
    detail = materialized.detail;
    blocks = materialized.blocks;
  }

  if (blocks.length > maxItems || encodedChars(blocks) > maxChars) {
    capped = true;
    let retained = blocks.slice(-maxItems);
    while (retained.length > 1 && encodedChars(retained) > maxChars) {
      retained = retained.slice(Math.max(1, Math.ceil(retained.length / 16)));
    }
    blocks = retained;
  }
  if (detail) {
    detail = {
      ...detail,
      blocks: [
        ...blocks,
        ...detail.blocks.filter(isFinal),
      ],
    };
  }
  const oldest = segments[0];
  const newest = segments.at(-1);
  return {
    projection: {
      segments,
      blocks,
      capped,
      hasMore: !!oldest?.hasMore,
      oldestCursor: oldest?.oldestCursor ?? null,
      hasNewer: !!newest?.hasNewer,
      newerCursor: newest?.newerCursor ?? null,
    },
    detail,
  };
}
