// IndexedDB cache: per-session turns + lastSeq.
//
// Opening the app restores history instantly from local storage and asks the
// wrapper only for the delta (seq > lastSeq) instead of replaying the whole
// buffer — that's what makes other apps feel instant vs. our "一段段补发".
//
// Writes are coalesced (turns change every frame during streaming).

import {
  isSessionControl, sessionControlTargetsSid, type SessionControl,
} from "./protocol.ts";
import type {
  Block, ProcessBlock, TextBlock, ToolBlock, Turn,
} from "./domain/conversation.ts";
import { mergeDetailWithLiveTail } from "./history-merge.ts";

const DB_NAME = "cc_remote_cache";
const STORE = "sessions";
const SCHEMA = 1;

// Bump when the cached turn shape changes in a way old entries can't be
// trusted (e.g. a past bug left tool-only turns without text). Old entries are
// ignored -> client falls back to a full buffer replay (text + tools restored).
// v6 adds assistant channels and structured process blocks. v7 binds cached
// turns to the backend's authoritative history revision so a destructive
// rewind or wrapper restart can never merge removed completed turns back in.
// v8 persists the revisioned v15 SessionControl snapshot so instant hydration
// cannot fall back to an unrevisioned terminal lock. v9 discards projections
// written by the old open-turn History merge, which could persist duplicate
// assistant blocks after switching away from and back to a running session.
// v10 separates the instant timeline skeleton from heavyweight detail so one
// image or tool output can no longer evict an otherwise valid completed turn.
// v11 attempted to retain Claude's transcript promptId as clientMsgId. Claude
// Code generates promptId internally, so it is not the browser Query.msg_id.
// v12 discards both older shapes; only wrapper-observed native UUID aliases may
// reconcile a canonical History turn with its optimistic browser row.
// v13 discards projections written by the delayed completed-message replay bug.
// The cache is only a rebuildable local projection; canonical History restores
// the same conversations without guessing whether repeated prose was pollution.
// v14 discards completed prompt-less replay orphans which an older matcher kept
// when several tool blocks legitimately shared one assistant message id.
// v15 discards projections written when a wrapper restart recovered the tail of
// an active Codex turn without re-announcing its exact logical message owner.
// v16 discards completed Codex projections written before canonical History
// persisted that exact owner. Hard refresh keeps IndexedDB, so a version fence
// is required to remove already-polluted duplicate layers once.
// v17 also discards legacy CLI projections whose historical user row did not
// yet reuse the adjacent native app-server item id emitted by the live stream.
// v18 discards active projections written while an evicted TurnBinding was
// seeded after (instead of before) its proven replay suffix. Those rows can
// contain a canonical History owner plus a second prompt-less live layer; the
// cache is rebuildable, so one clean reload is safer than shape-based guessing.
// v19 persists exact process presence independently from deferred detail size.
// Older rows can resurrect an empty "已处理" shell or hide a known process
// after a detail page is replaced, so they must be rebuilt from v36 History.
// v20 discards v19 Codex process clocks which may contain parser-time defaults.
// SQLite projection migrations cannot invalidate a browser's IndexedDB copy,
// and a hard refresh deliberately preserves that local cache.
// v21 discards Claude projections which may have persisted an interrupt marker
// as the browser alias for the following native user row.  The rebuilt v22
// History store fixes the owner, but IndexedDB needs its own explicit fence.
// v22 discards Codex turns whose bounded post-compact tail persisted a late
// process start. The wrapper now overlays an exact source-bound live clock.
// v23 discards Codex projections which may contain a prompt-less leading
// compaction row split from the real user message that owns the turn.
// v24 discards Claude rows whose completion clock could include late internal
// resume bookkeeping, and projections bloated by automatic full-detail paging.
// v25 reprojects native async questions instead of preserving plain-answer shells.
// v26 discards summaries where those questions hid ordinary unphased replies.
// v27 removes Claude turns split by native isMeta recovery prompts.
const CACHE_VER = 27;
const MAX_CACHE_SESSIONS = 64;
const MAX_CACHE_TURNS = 100;
const MAX_CACHE_BYTES = 2 * 1024 * 1024;
const CACHE_PROMPT_CHARS = 128 * 1024;
const CACHE_FINAL_TEXT_CHARS = 512 * 1024;
const CACHE_PROCESS_TEXT_CHARS = 256 * 1024;
const CACHE_PROCESS_DETAIL_CHARS = 512 * 1024;
const CACHE_IMAGE_CHARS = 384 * 1024;

interface ReplayRecord {
  sid: string;
  lastSeq: number;
  generation?: string;
  control?: SessionControl;
  savedAt: number;
}

function retainNewest(records: ReplayRecord[], record: ReplayRecord): void {
  records.push(record);
  records.sort((a, b) => b.savedAt - a.savedAt);
  if (records.length > MAX_CACHE_SESSIONS) records.length = MAX_CACHE_SESSIONS;
}

function clipCacheString(value: string | null | undefined, limit: number):
    string | null | undefined {
  if (value == null || value.length <= limit) return value;
  if (limit <= 1) return "…".slice(0, limit);
  const marker = "…";
  const content = limit - marker.length;
  const head = Math.ceil(content / 2);
  return value.slice(0, head) + marker + value.slice(-(content - head));
}

function compactCacheValue(
  value: unknown,
  limit: number,
  depth = 0,
): unknown {
  if (value == null || typeof value === "boolean"
      || typeof value === "number") return value;
  if (typeof value === "string") return clipCacheString(value, limit);
  if (depth >= 2) return "…";
  if (Array.isArray(value)) {
    return value.slice(0, 16).map((item) =>
      compactCacheValue(item, Math.max(64, Math.floor(limit / 16)), depth + 1));
  }
  if (typeof value !== "object") return String(value);
  const result: Record<string, unknown> = {};
  const entries = Object.entries(value as Record<string, unknown>).slice(0, 32);
  const itemLimit = Math.max(64, Math.floor(limit / Math.max(1, entries.length)));
  for (const [key, item] of entries) {
    result[clipCacheString(key, 256) ?? key] =
      compactCacheValue(item, itemLimit, depth + 1);
  }
  if (Object.keys(value as object).length > entries.length) {
    result._truncated = true;
  }
  return result;
}

function compactToolBlock(
  block: ToolBlock,
  detailBudget: { remaining: number },
): ToolBlock {
  const take = (value: string | null | undefined, limit: number) => {
    const available = Math.max(0, Math.min(limit, detailBudget.remaining));
    const clipped = clipCacheString(value, available);
    detailBudget.remaining -= clipped?.length ?? 0;
    return clipped;
  };
  const inputLimit = Math.min(8 * 1024, detailBudget.remaining);
  const input = compactCacheValue(block.input, inputLimit);
  detailBudget.remaining -= JSON.stringify(input)?.length ?? 0;
  return {
    ...block,
    input: input && typeof input === "object" && !Array.isArray(input)
      ? input as Record<string, unknown> : {},
    title: clipCacheString(block.title, 1024),
    server: clipCacheString(block.server, 1024) ?? undefined,
    progress: take(block.progress, 8 * 1024) ?? undefined,
    output: take(block.output, 16 * 1024) ?? undefined,
    diff: take(block.diff, 32 * 1024) ?? undefined,
    result: block.result ? {
      ...block.result,
      content: take(block.result.content, 16 * 1024) ?? "",
      summary: take(block.result.summary, 8 * 1024),
      diff: take(block.result.diff, 32 * 1024),
    } : undefined,
  };
}

function compactProcessBlock(
  block: ProcessBlock,
  detailBudget: { remaining: number },
): ProcessBlock {
  const take = (value: string | null | undefined, limit: number) => {
    const available = Math.max(0, Math.min(limit, detailBudget.remaining));
    const clipped = clipCacheString(value, available);
    detailBudget.remaining -= clipped?.length ?? 0;
    return clipped;
  };
  const inputLimit = Math.min(8 * 1024, detailBudget.remaining);
  const input = compactCacheValue(block.input, inputLimit);
  detailBudget.remaining -= JSON.stringify(input)?.length ?? 0;
  return {
    ...block,
    title: clipCacheString(block.title, 1024) || "处理事件",
    summary: take(block.summary, 8 * 1024),
    detail: take(block.detail, 16 * 1024),
    input: input && typeof input === "object" && !Array.isArray(input)
      ? input as Record<string, unknown> : null,
    output: take(block.output, 16 * 1024),
    diff: take(block.diff, 32 * 1024),
    progress: take(block.progress, 8 * 1024),
    server: clipCacheString(block.server, 1024),
    tool: clipCacheString(block.tool, 1024),
    command: take(block.command, 16 * 1024),
    cwd: take(block.cwd, 4 * 1024),
    explanation: take(block.explanation, 8 * 1024),
    plan: block.plan?.slice(0, 64).map((entry) => ({
      ...entry,
      step: take(entry.step, 2 * 1024) || "（空步骤）",
    })),
  };
}

/** Store the same lightweight local projection a native conversation client
 * paints on launch: every visible timeline identity survives, while attachment
 * bytes, raw cursor pages and heavyweight outputs remain in their independently
 * paged authoritative stores. A single large field can therefore never evict
 * the whole turn and make the middle of a completed process disappear. */
function projectTurnForCache(turn: Turn): Turn {
  const finalBudget = { remaining: CACHE_FINAL_TEXT_CHARS };
  const processTextBudget = { remaining: CACHE_PROCESS_TEXT_CHARS };
  const detailBudget = { remaining: CACHE_PROCESS_DETAIL_CHARS };
  const completedDetailAuthoritative = turn.done
    && !turn.detailRestorePending
    && !turn.detailRestoreIncomplete;
  const projectionWithArchive = turn.detailProjection || turn.liveSpillBlocks
    ? mergeDetailWithLiveTail(
        turn.detailProjection?.blocks ?? [],
        turn.liveSpillBlocks ?? [],
        completedDetailAuthoritative,
      )
    : [];
  const projectionBlocks = projectionWithArchive.length > 0
    ? mergeDetailWithLiveTail(
        projectionWithArchive,
        turn.blocks,
        completedDetailAuthoritative,
      )
    : turn.blocks;
  const finalBlocks = turn.blocks.filter((block): block is TextBlock =>
    block.kind === "text" && block.channel === "final");
  const sourceBlocks = [...projectionBlocks];
  for (const finalBlock of finalBlocks) {
    if (!sourceBlocks.some((block) =>
      block.kind === "text" && block.message_id === finalBlock.message_id)) {
      sourceBlocks.push(finalBlock);
    }
  }
  const blocks = sourceBlocks.map((block): Block => {
    if (block.kind === "tool") return compactToolBlock(block, detailBudget);
    if (block.kind === "process") {
      return compactProcessBlock(block, detailBudget);
    }
    const budget = block.channel === "final" ? finalBudget : processTextBudget;
    const perBlock = block.channel === "final" ? 256 * 1024 : 16 * 1024;
    const available = Math.max(0, Math.min(perBlock, budget.remaining));
    const text = clipCacheString(block.text, available) ?? "";
    budget.remaining -= text.length;
    return { ...block, text };
  });
  const deferredBlocks = blocks.filter((block) =>
    block.kind !== "text" || block.channel !== "final").length;
  const imageChars = turn.images?.reduce(
    (total, image) => total + image.media_type.length + image.data.length, 0);
  const images = imageChars != null && imageChars <= CACHE_IMAGE_CHARS
    ? turn.images?.map((image) => ({ ...image }))
    : undefined;
  return {
    ...turn,
    prompt: clipCacheString(turn.prompt, CACHE_PROMPT_CHARS) ?? "",
    error: clipCacheString(turn.error, 32 * 1024) ?? undefined,
    progress: clipCacheString(turn.progress, 8 * 1024) ?? undefined,
    blocks,
    detailReasons: turn.detailReasons
      ? [...turn.detailReasons] : undefined,
    // Keep a small optimistic attachment set for a flicker-free first paint.
    // Larger bodies stay in the transcript image store and are refilled through
    // History image references instead of evicting the whole turn.
    images,
    imageRefs: turn.imageRefs?.slice(0, 64).map((image) => ({ ...image })),
    files: turn.files?.slice(0, 64).map((file) => ({
      filename: clipCacheString(file.filename, 16 * 1024) ?? "",
      data: "",
    })),
    detailEventCount: Math.max(turn.detailEventCount ?? 0, deferredBlocks),
    detailLoaded: false,
    detailLoading: false,
    detailError: undefined,
    detailRetryBefore: undefined,
    detailRetryDirection: undefined,
    detailResetPending: false,
    detailProjection: undefined,
    detailHasMore: false,
    detailOldestCursor: null,
    detailHasNewer: false,
    detailNewerCursor: null,
    detailAutoLoad: false,
    detailRestorePending: false,
    detailRestoreOpen: false,
    detailRestoreIncomplete: false,
    liveSpillBlocks: undefined,
    liveSpillRefreshCount: undefined,
  };
}

function projectTurnSkeletonForCache(turn: Turn): Turn {
  let finalRemaining = 128 * 1024;
  let processRemaining = 128 * 1024;
  const blocks = turn.blocks.map((block): Block => {
    if (block.kind === "text") {
      const final = block.channel === "final";
      const remaining = final ? finalRemaining : processRemaining;
      const text = clipCacheString(
        block.text, Math.min(final ? 32 * 1024 : 2 * 1024, remaining)) ?? "";
      if (final) finalRemaining -= text.length;
      else processRemaining -= text.length;
      return {
        ...block,
        message_id: clipCacheString(block.message_id, 1024) ?? "",
        text,
      };
    }
    if (block.kind === "tool") {
      return {
        kind: "tool",
        message_id: clipCacheString(block.message_id, 1024) ?? "",
        tool_use_id: clipCacheString(block.tool_use_id, 1024) ?? "",
        tool: clipCacheString(block.tool, 512) ?? "",
        input: compactCacheValue(block.input, 1024) as Record<string, unknown>,
        category: block.category,
        title: clipCacheString(block.title, 512),
        parent_id: clipCacheString(block.parent_id, 1024),
        server: clipCacheString(block.server, 512),
        progress: clipCacheString(block.progress, 1024) ?? undefined,
        result: block.result ? {
          content: "",
          is_error: block.result.is_error,
          truncated: true,
          status: block.result.status,
          summary: clipCacheString(block.result.summary, 1024),
          exit_code: block.result.exit_code,
          duration_ms: block.result.duration_ms,
        } : undefined,
        done: block.done,
      };
    }
    return {
      kind: "process",
      item_id: clipCacheString(block.item_id, 1024) ?? "",
      processKind: block.processKind,
      phase: block.phase,
      status: block.status,
      turn_id: clipCacheString(block.turn_id, 1024),
      parent_id: clipCacheString(block.parent_id, 1024),
      title: clipCacheString(block.title, 512) || "处理事件",
      summary: clipCacheString(block.summary, 1024),
      progress: clipCacheString(block.progress, 1024),
      server: clipCacheString(block.server, 512),
      tool: clipCacheString(block.tool, 512),
      command: clipCacheString(block.command, 1024),
      cwd: clipCacheString(block.cwd, 1024),
      exit_code: block.exit_code,
      duration_ms: block.duration_ms,
      truncated: true,
      plan: block.plan?.slice(0, 32).map((entry) => ({
        ...entry,
        step: clipCacheString(entry.step, 512) || "（空步骤）",
      })),
      done: block.done,
    };
  });
  return {
    ...turn,
    prompt: clipCacheString(turn.prompt, 16 * 1024) ?? "",
    error: clipCacheString(turn.error, 8 * 1024) ?? undefined,
    progress: clipCacheString(turn.progress, 2 * 1024) ?? undefined,
    blocks,
    images: undefined,
    imageRefs: turn.imageRefs?.slice(0, 32).map((image) => ({ ...image })),
    files: turn.files?.slice(0, 32).map((file) => ({
      filename: clipCacheString(file.filename, 1024) ?? "",
      data: "",
    })),
    liveSpillBlocks: undefined,
    liveSpillRefreshCount: undefined,
  };
}

function cacheTurn(value: unknown): unknown {
  if (!value || typeof value !== "object" || Array.isArray(value)) return value;
  const record = value as Record<string, unknown>;
  if (typeof record.id === "string"
      && typeof record.prompt === "string"
      && typeof record.done === "boolean"
      && Array.isArray(record.blocks)) {
    return projectTurnForCache(value as Turn);
  }
  if (!Array.isArray(record.files)) return value;
  return {
    ...record,
    files: record.files.map((file) => {
      const filename = file && typeof file === "object"
        && typeof (file as Record<string, unknown>).filename === "string"
        ? (file as Record<string, unknown>).filename as string
        : "";
      return { filename, data: "" };
    }),
  };
}

/** Bound the structured clone written for one session. */
export function boundCachedTurns(turns: unknown[]): unknown[] {
  const candidates = turns.slice(-MAX_CACHE_TURNS);
  const kept: unknown[] = [];
  let bytes = 2; // []
  for (let i = candidates.length - 1; i >= 0; i--) {
    // Project lazily newest-first. A large current turn usually fills most of
    // the instant-paint budget, so walking and cloning every older page on each
    // 400 ms streaming flush would create avoidable main-thread work.
    let candidate = cacheTurn(candidates[i]);
    let encoded: string | undefined;
    try { encoded = JSON.stringify(candidate); } catch { continue; }
    if (encoded === undefined) continue;
    let size = new TextEncoder().encode(encoded).byteLength + 1;
    if (size > MAX_CACHE_BYTES && candidate
        && typeof candidate === "object" && !Array.isArray(candidate)
        && typeof (candidate as Partial<Turn>).id === "string"
        && typeof (candidate as Partial<Turn>).prompt === "string"
        && typeof (candidate as Partial<Turn>).done === "boolean"
        && Array.isArray((candidate as Partial<Turn>).blocks)) {
      candidate = projectTurnSkeletonForCache(candidate as Turn);
      try { encoded = JSON.stringify(candidate); } catch { continue; }
      if (encoded === undefined) continue;
      size = new TextEncoder().encode(encoded).byteLength + 1;
    }
    if (size > MAX_CACHE_BYTES || bytes + size > MAX_CACHE_BYTES) continue;
    kept.unshift(candidate);
    bytes += size;
  }
  return kept;
}

function cachedTurnRecord(value: unknown): Record<string, unknown> | null {
  return value != null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown> : null;
}

function cachedCompactionTurnIds(turn: Record<string, unknown>): Set<string> {
  const blocks = Array.isArray(turn.blocks) ? turn.blocks : [];
  return new Set(blocks.flatMap((value) => {
    const block = cachedTurnRecord(value);
    return block?.kind === "process"
      && block.processKind === "compaction"
      && typeof block.turn_id === "string"
      && block.turn_id
      ? [block.turn_id] : [];
  }));
}

function cachedNativeTurnIds(turn: Record<string, unknown>): Set<string> {
  return new Set([
    turn.forkPointId,
    turn.liveTaskId,
    turn.codexTurnId,
  ].filter((value): value is string => typeof value === "string" && !!value));
}

/** Identify only the exact v15 corruption produced when a reconnect replayed
 * a compacted Codex tail after its original TurnBinding cursor.  This is not a
 * general duplicate matcher: legitimate optimistic steers may briefly leave
 * two open rows, and repeated text/native task ids are never identity proof. */
export function hasOrphanedActiveCompactionProjection(
  turns: unknown,
): boolean {
  if (!Array.isArray(turns)) return false;
  const records = turns.map(cachedTurnRecord)
    .filter((turn): turn is Record<string, unknown> => turn !== null);
  const orphans = records.filter((turn) => {
    if (turn.done !== false || turn.prompt !== ""
        || typeof turn.id !== "string" || !turn.id) return false;
    if ([
      turn.clientMsgId,
      turn.historyTurnId,
      turn.forkPointId,
      turn.checkpointId,
      turn.codexTurnId,
      turn.liveTaskId,
    ].some((value) => typeof value === "string" && !!value)) return false;
    const blocks = Array.isArray(turn.blocks) ? turn.blocks : [];
    const ownsAssistantEnvelope = blocks.some((value) => {
      const block = cachedTurnRecord(value);
      return block?.kind === "text" && block.message_id === turn.id;
    });
    return ownsAssistantEnvelope
      && cachedCompactionTurnIds(turn).size === 1;
  });
  if (orphans.length !== 1) return false;

  const orphan = orphans[0];
  const [compactionTurnId] = cachedCompactionTurnIds(orphan);
  const canonical = records.filter((turn) => {
    if (turn === orphan || turn.done !== false
        || typeof turn.prompt !== "string" || !turn.prompt) return false;
    return cachedNativeTurnIds(turn).has(compactionTurnId)
      && cachedCompactionTurnIds(turn).has(compactionTurnId);
  });
  return canonical.length === 1;
}

async function pruneCacheStore(d: IDBDatabase): Promise<void> {
  await new Promise<void>((resolve) => {
    const tx = d.transaction(STORE, "readwrite");
    const store = tx.objectStore(STORE);
    const newest: Array<{ key: IDBValidKey; savedAt: number }> = [];
    const req = store.openCursor();
    req.onsuccess = () => {
      const cur = req.result;
      if (!cur) return;
      const value = cur.value as (CachedSession & { v?: number }) | undefined;
      if (!value || value.v !== CACHE_VER
          || hasOrphanedActiveCompactionProjection(value.turns)) {
        cur.delete();
        cur.continue();
        return;
      }
      newest.push({
        key: cur.key,
        savedAt: typeof value.savedAt === "number" ? value.savedAt : 0,
      });
      newest.sort((a, b) => b.savedAt - a.savedAt);
      if (newest.length > MAX_CACHE_SESSIONS) {
        const evicted = newest.pop();
        if (evicted) {
          if (evicted.key === cur.key) cur.delete();
          else store.delete(evicted.key);
        }
      }
      cur.continue();
    };
    tx.oncomplete = () => resolve();
    tx.onerror = () => resolve();
    tx.onabort = () => resolve();
  });
}

export interface CachedSession {
  turns: unknown[];
  lastSeq: number;
  revision: string;
  generation?: string;
  control?: SessionControl | null;
  /** Paint-only proof that the saved projection reached the first turn.
   * It never carries or authorizes a pagination cursor. */
  historyAtStart?: boolean;
  savedAt: number;
}

/** Validate and bind a cache control snapshot to its IndexedDB row key. */
export function controlForCachedSession(
  sessionId: string, value: unknown,
): SessionControl | undefined {
  return isSessionControl(value) && sessionControlTargetsSid(value, sessionId)
    ? value : undefined;
}

let dbPromise: Promise<IDBDatabase> | null = null;
function db(): Promise<IDBDatabase> {
  if (dbPromise) return dbPromise;
  dbPromise = new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, SCHEMA);
    req.onupgradeneeded = () => {
      const d = req.result;
      if (!d.objectStoreNames.contains(STORE)) d.createObjectStore(STORE);
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
  return dbPromise;
}

export async function loadSession(sessionId: string): Promise<CachedSession | null> {
  if (typeof indexedDB === "undefined" || invalidatedSessions.has(sessionId)) {
    return null;
  }
  try {
    const d = await db();
    return await new Promise<CachedSession | null>((resolve) => {
      const tx = d.transaction(STORE, "readonly");
      const req = tx.objectStore(STORE).get(sessionId);
      req.onsuccess = () => {
        const r = req.result as (CachedSession & { v?: number }) | undefined;
        // Ignore stale caches from before the current shape (v must match).
        if (invalidatedSessions.has(sessionId) || !r || r.v !== CACHE_VER
            || typeof r.revision !== "string" || !r.revision
            || (r.control != null && !isSessionControl(r.control))
            || hasOrphanedActiveCompactionProjection(r.turns)) {
          resolve(null);
          return;
        }
        resolve({
          ...r,
          control: controlForCachedSession(sessionId, r.control),
        });
      };
      req.onerror = () => resolve(null);
    });
  } catch {
    return null;
  }
}

/** All cached sessions' last-seen seq, for seeding reconnect cursors so the
 *  wrapper replays only the DELTA (seq > lastSeq) instead of flooding the full
 *  history of every resident session on every reconnect. */
export async function loadAllReplayState(): Promise<{
  cursors: Record<string, number>;
  generations: Record<string, string>;
  controls: Record<string, SessionControl>;
}> {
  if (typeof indexedDB === "undefined") {
    return { cursors: {}, generations: {}, controls: {} };
  }
  try {
    const d = await db();
    const replay = await new Promise<{
      cursors: Record<string, number>;
      generations: Record<string, string>;
      controls: Record<string, SessionControl>;
    }>((resolve) => {
      const records: ReplayRecord[] = [];
      const req = d.transaction(STORE, "readonly").objectStore(STORE).openCursor();
      req.onsuccess = () => {
        const cur = req.result;
        if (!cur) {
          const cursors: Record<string, number> = {};
          const generations: Record<string, string> = {};
          const controls: Record<string, SessionControl> = {};
          for (const record of records) {
            if (record.lastSeq > 0) {
              cursors[record.sid] = record.lastSeq;
            }
            if (record.generation) generations[record.sid] = record.generation;
            if (record.control) controls[record.sid] = record.control;
          }
          resolve({ cursors, generations, controls });
          return;
        }
        const r = cur.value as (CachedSession & { v?: number }) | undefined;
        if (r && r.v === CACHE_VER
            && typeof r.revision === "string" && r.revision
            && typeof r.lastSeq === "number"
            && !hasOrphanedActiveCompactionProjection(r.turns)
            && (r.lastSeq > 0 || isSessionControl(r.control))) {
          const sid = String(cur.key);
          const control = controlForCachedSession(sid, r.control);
          retainNewest(records, {
            sid,
            lastSeq: r.lastSeq,
            generation: typeof r.generation === "string" && r.generation
              ? r.generation
              : (control?.generation ?? undefined),
            control,
            savedAt: typeof r.savedAt === "number" ? r.savedAt : 0,
          });
        }
        cur.continue();
      };
      req.onerror = () => resolve({ cursors: {}, generations: {}, controls: {} });
    });
    // Queue a bounded cleanup after the readonly cursor transaction. This also
    // repairs databases created by older builds that never evicted sessions.
    void pruneCacheStore(d);
    return replay;
  } catch {
    return { cursors: {}, generations: {}, controls: {} };
  }
}

const pending = new Map<string, {
  sid: string; turns: unknown[]; lastSeq: number; revision: string;
  generation?: string;
  control?: SessionControl | null;
  historyAtStart?: boolean;
  epoch: number;
}>();
// A destructive history mutation must invalidate both the committed IDB row
// and any debounce/in-flight write that captured the pre-mutation turns.
const sessionEpochs = new Map<string, number>();
const invalidatedSessions = new Set<string>();
const invalidationTasks = new Map<string, {
  epoch: number;
  task: Promise<void>;
}>();
let saveTimer: ReturnType<typeof setTimeout> | null = null;

function sessionEpoch(sessionId: string): number {
  return sessionEpochs.get(sessionId) ?? 0;
}

/** Coalesced write — call freely on every turns change; actual IDB write is
 *  debounced 400ms so streaming doesn't hammer IndexedDB. */
export function saveSession(
  sessionId: string, turns: unknown[], lastSeq: number, revision: string,
  generation?: string, control?: SessionControl | null,
  historyAtStart?: boolean,
): void {
  if (typeof indexedDB === "undefined" || !sessionId || !revision) return;
  if (invalidatedSessions.has(sessionId)) return;
  if (!pending.has(sessionId) && pending.size >= MAX_CACHE_SESSIONS) {
    const oldest = pending.keys().next().value as string | undefined;
    if (oldest) pending.delete(oldest);
  }
  pending.set(sessionId, {
    sid: sessionId,
    turns,
    lastSeq,
    revision,
    generation,
    control: controlForCachedSession(sessionId, control),
    historyAtStart: historyAtStart === true,
    epoch: sessionEpoch(sessionId),
  });
  if (saveTimer) return;
  saveTimer = setTimeout(flush, 400);
}

async function flush(): Promise<void> {
  saveTimer = null;
  const jobs = Array.from(pending.values());
  pending.clear();
  if (!jobs.length) return;
  try {
    const d = await db();
    await new Promise<void>((resolve) => {
      const tx = d.transaction(STORE, "readwrite");
      const store = tx.objectStore(STORE);
      for (const job of jobs) {
        if (invalidatedSessions.has(job.sid)
            || job.epoch !== sessionEpoch(job.sid)) continue;
        const turns = boundCachedTurns(job.turns);
        if (hasOrphanedActiveCompactionProjection(turns)) {
          store.delete(job.sid);
          continue;
        }
        store.put(
          { v: CACHE_VER, turns, lastSeq: job.lastSeq,
            revision: job.revision, generation: job.generation,
            control: job.control,
            // If cache bounding dropped an older row, this projection no
            // longer proves the conversation start even when the live runtime
            // had loaded it before saving.
            historyAtStart: job.historyAtStart === true
              && turns.length === job.turns.length,
            savedAt: Date.now() }, job.sid);
      }
      tx.oncomplete = () => resolve();
      tx.onerror = () => resolve();
    });
    await pruneCacheStore(d);
  } catch { /* ignore — cache is best-effort */ }
  if (pending.size && !saveTimer) saveTimer = setTimeout(flush, 400);
}

/** Remove one session after a destructive transcript mutation.

    The epoch also makes a write already copied out of `pending` harmless. Keep
    reads/writes blocked until App observes a subsequent authoritative History. */
export async function invalidateSessionCache(sessionId: string): Promise<void> {
  if (!sessionId) return;
  const epoch = sessionEpoch(sessionId) + 1;
  sessionEpochs.set(sessionId, epoch);
  invalidatedSessions.add(sessionId);
  pending.delete(sessionId);
  const task = (async () => {
    if (typeof indexedDB === "undefined") return;
    try {
      const d = await db();
      await new Promise<void>((resolve) => {
        const tx = d.transaction(STORE, "readwrite");
        tx.objectStore(STORE).delete(sessionId);
        tx.oncomplete = () => resolve();
        tx.onerror = () => resolve();
        tx.onabort = () => resolve();
      });
    } catch { /* best-effort cache invalidation */ }
  })();
  invalidationTasks.set(sessionId, { epoch, task });
  await task;
  if (invalidationTasks.get(sessionId)?.task === task) {
    invalidationTasks.delete(sessionId);
  }
}

/** Re-enable future cache writes after an authoritative History replacement. */
export async function allowSessionCache(sessionId: string): Promise<void> {
  const epoch = sessionEpoch(sessionId);
  await invalidationTasks.get(sessionId)?.task;
  if (sessionEpoch(sessionId) === epoch) invalidatedSessions.delete(sessionId);
}

/** Explicit logout removes locally cached prompts and base64 images. */
export async function clearCache(): Promise<void> {
  pending.clear();
  invalidatedSessions.clear();
  sessionEpochs.clear();
  invalidationTasks.clear();
  if (saveTimer) {
    clearTimeout(saveTimer);
    saveTimer = null;
  }
  if (typeof indexedDB === "undefined") return;
  try {
    const d = await db();
    await new Promise<void>((resolve) => {
      const tx = d.transaction(STORE, "readwrite");
      tx.objectStore(STORE).clear();
      tx.oncomplete = () => resolve();
      tx.onerror = () => resolve();
      tx.onabort = () => resolve();
    });
  } catch { /* best-effort local cleanup */ }
}
