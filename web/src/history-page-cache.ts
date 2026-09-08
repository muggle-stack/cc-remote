import {
  canonicalTurnId,
  type HistoryBrowsePage,
} from "./history-browse.ts";
import type { Block, Turn } from "./domain/conversation.ts";

/** Deliberately independent from cache.ts. Deep-history browsing is best-effort
 * page storage and must never make an upgrade/failure of the replay/session
 * cache prevent startup. */
export const HISTORY_PAGE_CACHE_DB_NAME = "cc_remote_history_page_cache";
const HISTORY_PAGE_CACHE_DB_VERSION = 2;
const HISTORY_PAGE_CACHE_STORE = "pages";
const HISTORY_PAGE_CACHE_SCOPE_INDEX = "scope";
const HISTORY_PAGE_CACHE_SESSION_INDEX = "session";
const HISTORY_PAGE_CACHE_LRU_INDEX = "lru";
const DEFAULT_HISTORY_PAGE_CACHE_BYTES = 64 * 1024 * 1024;
// v4 preserves a payload-free context-compaction identity shell. Older page
// records can otherwise turn a repaired compact orphan into an unprovable
// empty row after a hard refresh, so they must be rebuilt from History.
// v5 stores v36's exact/unknown process-detail state. A v4 page can only infer
// process presence from a generic deferred-event count and is unsafe to paint.
// v6 restores async question metadata. v7 rebuilds pages where those questions
// suppressed an ordinary unphased reply; the source revision alone cannot tell.
// v8 preserves native model-fallback notices and immutable turn-change metadata.
// v9 discards old official-summary pages which omitted their file lists.
const RECORD_VERSION = 9;

export interface HistoryPageCacheSessionScope {
  machineId: string;
  engine: string;
  space: string;
  sid: string;
}

export interface HistoryPageCacheScope extends HistoryPageCacheSessionScope {
  revision: string;
}

export interface HistoryPageCacheStoredRecord {
  version: 1 | 2 | 3 | 4 | typeof RECORD_VERSION;
  key: string;
  scopeKey: string;
  sessionKey: string;
  machineId: string;
  engine: string;
  space: string;
  sid: string;
  revision: string;
  pageKey: string;
  page: HistoryBrowsePage;
  savedAt: number;
  byteSize: number;
}

export interface HistoryPageCacheStorage {
  read(key: string): Promise<unknown | null>;
  write(
    record: HistoryPageCacheStoredRecord,
    maxBytes: number,
  ): Promise<{ evicted: number }>;
  deleteKey(key: string): Promise<void>;
  deleteRevision(scopeKey: string): Promise<void>;
  deleteSession(sessionKey: string): Promise<void>;
  clear(): Promise<void>;
}

export type HistoryPageCacheFailureReason =
  | "unavailable"
  | "quota"
  | "corrupt"
  | "invalidated"
  | "error";

export type HistoryPageCacheResult =
  | { ok: true; evicted?: number }
  | { ok: false; reason: HistoryPageCacheFailureReason };

export interface HistoryPageCacheOptions {
  maxBytes?: number;
  indexedDB?: IDBFactory | null;
  storage?: HistoryPageCacheStorage | null;
  now?: () => number;
}

function scopeValues(scope: HistoryPageCacheSessionScope): string[] {
  return [
    scope.machineId,
    scope.engine,
    scope.space,
    scope.sid,
  ];
}

function validScope(scope: HistoryPageCacheSessionScope): boolean {
  return scopeValues(scope).every((value) =>
    typeof value === "string" && value.length > 0);
}

export function historyPageCacheSessionKey(
  scope: HistoryPageCacheSessionScope,
): string {
  return JSON.stringify(scopeValues(scope));
}

export function historyPageCacheScopeKey(
  scope: HistoryPageCacheScope,
): string {
  return JSON.stringify([...scopeValues(scope), scope.revision]);
}

export function historyPageCachePageKey(
  scope: HistoryPageCacheScope,
  pageKey: string,
): string {
  return JSON.stringify([...scopeValues(scope), scope.revision, pageKey]);
}

function sanitizeTurn(turn: Turn): Turn {
  const summaryBlocks = turn.blocks.flatMap((block): Block[] => {
    if (block.kind === "text" && block.channel === "final") {
      return [{ ...block }];
    }
    // Context compaction is lightweight narrative metadata, and its native
    // turn id is the proof used to repair the historical standalone-row bug.
    // Preserve a payload-free shell across page eviction/cache reload, along
    // with native model-fallback notices; heavy process bodies stay detail-only.
    if (block.kind === "process" && (block.processKind === "compaction"
        || (block.processKind === "model" && block.tool === "model_refusal_fallback"))) {
      return [{
        kind: "process" as const,
        item_id: block.item_id,
        processKind: block.processKind,
        phase: block.phase,
        status: block.status,
        turn_id: block.turn_id,
        parent_id: block.parent_id,
        title: block.title,
        tool: block.tool,
        summary: block.summary,
        duration_ms: block.duration_ms,
        truncated: block.truncated,
        done: block.done,
      }];
    }
    return [];
  });
  const deferredBlocks = turn.blocks.length - summaryBlocks.length;
  return {
    ...turn,
    // Tool/process/thinking bodies are intentionally deferred to
    // GetTurnDetail. Keeping their stripped shells produced dozens of
    // expandable "运行命令" rows with empty bodies after an IndexedDB paint.
    blocks: summaryBlocks,
    detailReasons: turn.detailReasons
      ? [...turn.detailReasons] : undefined,
    // Summary pages keep canonical metadata only. Attachment bytes are fetched
    // lazily through GetHistoryImage when this row re-enters the viewport.
    images: undefined,
    files: turn.files?.map((file) => ({
      filename: file.filename,
      data: "",
    })),
    // Heavy detail is independently rebuildable and is never page-cached.
    detailEventCount: Math.max(
      turn.detailEventCount ?? 0, deferredBlocks),
    detailLoaded: false,
    detailLoading: false,
    detailError: undefined,
    detailRetryBefore: undefined,
    detailRetryDirection: undefined,
    detailResetPending: false,
    detailProjection: undefined,
    detailHasMore: undefined,
    detailOldestCursor: undefined,
    detailHasNewer: undefined,
    detailNewerCursor: undefined,
    detailAutoLoad: false,
  };
}

/** Enforce the storage contract even when callers accidentally pass runtime
 * turns instead of summary turns. */
export function sanitizeHistoryPageForCache(
  page: HistoryBrowsePage,
): HistoryBrowsePage {
  return {
    pageKey: page.pageKey,
    turns: page.turns.map(sanitizeTurn),
    hasOlder: !!page.hasOlder,
    olderCursor: page.olderCursor ?? null,
    hasNewer: page.isLatest ? false : !!page.hasNewer || !!page.newerPageKey,
    newerPageKey: page.isLatest ? null : page.newerPageKey ?? null,
    isLatest: !!page.isLatest,
  };
}

function encodedBytes(value: unknown): number {
  try {
    const encoded = JSON.stringify(value);
    return encoded == null ? 0 : new TextEncoder().encode(encoded).byteLength;
  } catch {
    return Number.MAX_SAFE_INTEGER;
  }
}

function validTurn(value: unknown): value is Turn {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const turn = value as Partial<Turn>;
  return typeof turn.id === "string" && turn.id.length > 0
    && typeof turn.prompt === "string"
    && typeof turn.done === "boolean"
    && Array.isArray(turn.blocks);
}

function validPage(value: unknown): value is HistoryBrowsePage {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const page = value as Partial<HistoryBrowsePage>;
  return typeof page.pageKey === "string" && page.pageKey.length > 0
    && Array.isArray(page.turns) && page.turns.every(validTurn)
    && typeof page.hasOlder === "boolean"
    && (page.olderCursor == null || typeof page.olderCursor === "string")
    && (page.newerPageKey == null || typeof page.newerPageKey === "string");
}

function validRecord(
  value: unknown,
  expected: {
    key: string;
    scopeKey: string;
    sessionKey: string;
    pageKey: string;
    engine: string;
  },
): value is HistoryPageCacheStoredRecord {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const record = value as Partial<HistoryPageCacheStoredRecord>;
  return record.version === RECORD_VERSION
    && record.key === expected.key
    && record.scopeKey === expected.scopeKey
    && record.sessionKey === expected.sessionKey
    && record.pageKey === expected.pageKey
    && typeof record.savedAt === "number"
    && Number.isFinite(record.savedAt)
    && typeof record.byteSize === "number"
    && Number.isFinite(record.byteSize)
    && record.byteSize >= 0
    && validPage(record.page);
}

/** Merge repeated partial evictions of one logical page. New input wins a
 * canonical overlap, while existing-only rows remain recoverable. */
function mergePageTurns(
  existing: readonly Turn[],
  incoming: readonly Turn[],
): Turn[] {
  const turns = [...existing];
  const indexes = new Map<string, number>();
  turns.forEach((turn, index) => indexes.set(canonicalTurnId(turn), index));
  for (const turn of incoming) {
    const key = canonicalTurnId(turn);
    const index = indexes.get(key);
    if (index == null) {
      indexes.set(key, turns.length);
      turns.push(turn);
    } else {
      turns[index] = turn;
    }
  }
  turns.sort((left, right) => {
    if (left.ts != null && right.ts != null && left.ts !== right.ts) {
      return left.ts - right.ts;
    }
    return 0;
  });
  return turns;
}

function mergePages(
  existing: HistoryBrowsePage | null,
  incoming: HistoryBrowsePage,
): HistoryBrowsePage {
  if (!existing) return incoming;
  return {
    ...existing,
    ...incoming,
    turns: mergePageTurns(existing.turns, incoming.turns),
  };
}

class HistoryPageQuotaError extends Error {
  override name = "QuotaExceededError";
}

function errorReason(error: unknown): HistoryPageCacheFailureReason {
  const name = error && typeof error === "object"
    && "name" in error ? String(error.name) : "";
  if (name === "QuotaExceededError" || name === "NS_ERROR_DOM_QUOTA_REACHED") {
    return "quota";
  }
  if ([
    "SecurityError",
    "InvalidStateError",
    "NotAllowedError",
    "UnknownError",
  ].includes(name)) {
    return "unavailable";
  }
  return "error";
}

function idbRequest<T>(request: IDBRequest<T>): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error ?? new Error("IndexedDB request failed"));
  });
}

function transactionDone(transaction: IDBTransaction): Promise<void> {
  return new Promise<void>((resolve, reject) => {
    transaction.oncomplete = () => resolve();
    transaction.onerror = () => reject(
      transaction.error ?? new Error("IndexedDB transaction failed"),
    );
    transaction.onabort = () => reject(
      transaction.error ?? new Error("IndexedDB transaction aborted"),
    );
  });
}

interface HistoryPageCacheLruEntry {
  primaryKey: string;
  savedAt: number;
  byteSize: number;
}

function collectPrimaryKeys(
  request: IDBRequest<IDBCursor | null>,
): Promise<IDBValidKey[]> {
  return new Promise<IDBValidKey[]>((resolve, reject) => {
    const keys: IDBValidKey[] = [];
    request.onsuccess = () => {
      const cursor = request.result;
      if (!cursor) {
        resolve(keys);
        return;
      }
      keys.push(cursor.primaryKey);
      cursor.continue();
    };
    request.onerror = () => reject(
      request.error ?? new Error("IndexedDB key cursor failed"),
    );
  });
}

function lruEntries(
  request: IDBRequest<IDBCursor | null>,
): Promise<HistoryPageCacheLruEntry[]> {
  return new Promise<HistoryPageCacheLruEntry[]>((resolve, reject) => {
    const entries: HistoryPageCacheLruEntry[] = [];
    request.onsuccess = () => {
      const cursor = request.result;
      if (!cursor) {
        resolve(entries);
        return;
      }
      const key = cursor.key;
      const primaryKey = cursor.primaryKey;
      if (Array.isArray(key)
          && key.length === 3
          && typeof key[0] === "number"
          && Number.isFinite(key[0])
          && typeof key[1] === "number"
          && Number.isFinite(key[1])
          && key[1] >= 0
          && typeof key[2] === "string"
          && typeof primaryKey === "string"
          && key[2] === primaryKey) {
        entries.push({
          primaryKey,
          savedAt: key[0],
          byteSize: key[1],
        });
      }
      cursor.continue();
    };
    request.onerror = () => reject(
      request.error ?? new Error("IndexedDB LRU cursor failed"),
    );
  });
}

class IndexedDbHistoryPageStorage implements HistoryPageCacheStorage {
  private dbPromise: Promise<IDBDatabase> | null = null;
  private readonly factory: IDBFactory;

  constructor(factory: IDBFactory) {
    this.factory = factory;
  }

  private open(): Promise<IDBDatabase> {
    if (this.dbPromise) return this.dbPromise;
    this.dbPromise = new Promise<IDBDatabase>((resolve, reject) => {
      let request: IDBOpenDBRequest;
      try {
        request = this.factory.open(
          HISTORY_PAGE_CACHE_DB_NAME,
          HISTORY_PAGE_CACHE_DB_VERSION,
        );
      } catch (error) {
        reject(error);
        return;
      }
      request.onupgradeneeded = () => {
        const database = request.result;
        const store = database.objectStoreNames.contains(
            HISTORY_PAGE_CACHE_STORE,
          )
          ? request.transaction!.objectStore(HISTORY_PAGE_CACHE_STORE)
          : database.createObjectStore(
            HISTORY_PAGE_CACHE_STORE,
            { keyPath: "key" },
          );
        if (!store.indexNames.contains(HISTORY_PAGE_CACHE_SCOPE_INDEX)) {
          store.createIndex(
            HISTORY_PAGE_CACHE_SCOPE_INDEX,
            "scopeKey",
            { unique: false },
          );
        }
        if (!store.indexNames.contains(HISTORY_PAGE_CACHE_SESSION_INDEX)) {
          store.createIndex(
            HISTORY_PAGE_CACHE_SESSION_INDEX,
            "sessionKey",
            { unique: false },
          );
        }
        if (!store.indexNames.contains(HISTORY_PAGE_CACHE_LRU_INDEX)) {
          // The compound index exposes ordering and byte sizes through a key
          // cursor. Schema upgrades index legacy records for bounded accounting;
          // semantic record-version validation removes them lazily on read.
          store.createIndex(
            HISTORY_PAGE_CACHE_LRU_INDEX,
            ["savedAt", "byteSize", "key"],
            { unique: false },
          );
        }
      };
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => {
        this.dbPromise = null;
        reject(request.error ?? new Error("IndexedDB open failed"));
      };
      request.onblocked = () => {
        this.dbPromise = null;
        reject(new DOMException("IndexedDB open blocked", "InvalidStateError"));
      };
    });
    return this.dbPromise;
  }

  async read(key: string): Promise<unknown | null> {
    const database = await this.open();
    const transaction = database.transaction(
      HISTORY_PAGE_CACHE_STORE,
      "readonly",
    );
    const done = transactionDone(transaction);
    const request = transaction.objectStore(HISTORY_PAGE_CACHE_STORE).get(key);
    const value = await idbRequest(request);
    await done;
    return value ?? null;
  }

  async write(
    record: HistoryPageCacheStoredRecord,
    maxBytes: number,
  ): Promise<{ evicted: number }> {
    if (record.byteSize > maxBytes) throw new HistoryPageQuotaError();
    const database = await this.open();
    const transaction = database.transaction(
      HISTORY_PAGE_CACHE_STORE,
      "readwrite",
    );
    const done = transactionDone(transaction);
    const store = transaction.objectStore(HISTORY_PAGE_CACHE_STORE);
    let primaryKeys: IDBValidKey[];
    let candidates: HistoryPageCacheLruEntry[];
    try {
      [primaryKeys, candidates] = await Promise.all([
        collectPrimaryKeys(store.openKeyCursor()),
        lruEntries(store.index(HISTORY_PAGE_CACHE_LRU_INDEX).openKeyCursor()),
      ]);
    } catch (error) {
      try { transaction.abort(); } catch { /* transaction already failed */ }
      await done.catch(() => undefined);
      throw error;
    }
    const indexedKeys = new Set(candidates.map((candidate) =>
      candidate.primaryKey));
    // An entry which cannot participate in the size/LRU index is not a valid
    // cache record. Delete it instead of silently excluding its stored bytes.
    for (const key of primaryKeys) {
      if (key !== record.key
          && (typeof key !== "string" || !indexedKeys.has(key))) {
        store.delete(key);
      }
    }
    let total = record.byteSize;
    for (const candidate of candidates) {
      if (candidate.primaryKey === record.key) continue;
      total += candidate.byteSize;
    }
    candidates.sort((left, right) =>
      left.savedAt - right.savedAt
      || left.primaryKey.localeCompare(right.primaryKey));
    let evicted = 0;
    for (const candidate of candidates) {
      if (total <= maxBytes) break;
      if (candidate.primaryKey === record.key) continue;
      store.delete(candidate.primaryKey);
      total -= candidate.byteSize;
      evicted += 1;
    }
    if (total > maxBytes) {
      transaction.abort();
      await done.catch(() => undefined);
      throw new HistoryPageQuotaError();
    }
    store.put(record);
    await done;
    return { evicted };
  }

  async deleteKey(key: string): Promise<void> {
    const database = await this.open();
    const transaction = database.transaction(
      HISTORY_PAGE_CACHE_STORE,
      "readwrite",
    );
    transaction.objectStore(HISTORY_PAGE_CACHE_STORE).delete(key);
    await transactionDone(transaction);
  }

  private async deleteByIndex(indexName: string, value: string): Promise<void> {
    const database = await this.open();
    const transaction = database.transaction(
      HISTORY_PAGE_CACHE_STORE,
      "readwrite",
    );
    const store = transaction.objectStore(HISTORY_PAGE_CACHE_STORE);
    const request = store.index(indexName).openKeyCursor(value);
    request.onsuccess = () => {
      const cursor = request.result;
      if (!cursor) return;
      store.delete(cursor.primaryKey);
      cursor.continue();
    };
    await transactionDone(transaction);
  }

  deleteRevision(scopeKey: string): Promise<void> {
    return this.deleteByIndex(HISTORY_PAGE_CACHE_SCOPE_INDEX, scopeKey);
  }

  deleteSession(sessionKey: string): Promise<void> {
    return this.deleteByIndex(HISTORY_PAGE_CACHE_SESSION_INDEX, sessionKey);
  }

  async clear(): Promise<void> {
    const database = await this.open();
    const transaction = database.transaction(
      HISTORY_PAGE_CACHE_STORE,
      "readwrite",
    );
    transaction.objectStore(HISTORY_PAGE_CACHE_STORE).clear();
    await transactionDone(transaction);
  }
}

interface EpochSnapshot {
  global: number;
  session: number;
  revision: number;
}

export class HistoryPageCache {
  private readonly storage: HistoryPageCacheStorage | null;
  private readonly maxBytes: number;
  private readonly now: () => number;
  private globalEpoch = 0;
  private readonly sessionEpochs = new Map<string, number>();
  private readonly revisionEpochs = new Map<string, number>();
  private globalInvalidationFailed = false;
  private readonly failedSessionInvalidations = new Set<string>();
  private readonly failedRevisionInvalidations = new Map<string, string>();
  private readonly sessionInvalidations = new Map<string, Promise<void>>();
  private readonly revisionInvalidations = new Map<string, Promise<void>>();
  private readonly activeWrites = new Map<string, Set<Promise<void>>>();
  private readonly pageWriteQueues = new Map<string, Promise<void>>();
  private clearTask: Promise<void> | null = null;

  constructor(options: HistoryPageCacheOptions = {}) {
    this.maxBytes = Math.max(
      1,
      Math.floor(options.maxBytes ?? DEFAULT_HISTORY_PAGE_CACHE_BYTES),
    );
    this.now = options.now ?? (() => Date.now());
    if (Object.hasOwn(options, "storage")) {
      this.storage = options.storage ?? null;
      return;
    }
    const factory = Object.hasOwn(options, "indexedDB")
      ? options.indexedDB
      : typeof indexedDB === "undefined" ? null : indexedDB;
    this.storage = factory ? new IndexedDbHistoryPageStorage(factory) : null;
  }

  pageKey(scope: HistoryPageCacheScope, pageKey: string): string {
    return historyPageCachePageKey(scope, pageKey);
  }

  private snapshot(scope: HistoryPageCacheScope): EpochSnapshot {
    const sessionKey = historyPageCacheSessionKey(scope);
    const scopeKey = historyPageCacheScopeKey(scope);
    return {
      global: this.globalEpoch,
      session: this.sessionEpochs.get(sessionKey) ?? 0,
      revision: this.revisionEpochs.get(scopeKey) ?? 0,
    };
  }

  private snapshotMatches(
    scope: HistoryPageCacheScope,
    snapshot: EpochSnapshot,
  ): boolean {
    const current = this.snapshot(scope);
    return current.global === snapshot.global
      && current.session === snapshot.session
      && current.revision === snapshot.revision;
  }

  private invalidationFailed(scope: HistoryPageCacheScope): boolean {
    return this.globalInvalidationFailed
      || this.failedSessionInvalidations.has(
        historyPageCacheSessionKey(scope),
      )
      || this.failedRevisionInvalidations.has(
        historyPageCacheScopeKey(scope),
      );
  }

  private async waitForBarriers(
    scope: HistoryPageCacheScope,
  ): Promise<boolean> {
    const tasks = [
      this.clearTask,
      this.sessionInvalidations.get(historyPageCacheSessionKey(scope)),
      this.revisionInvalidations.get(historyPageCacheScopeKey(scope)),
    ].filter((task): task is Promise<void> => task != null);
    if (tasks.length) {
      await Promise.all(tasks.map((task) => task.catch(() => undefined)));
    }
    return !this.invalidationFailed(scope);
  }

  /** Serialize every read/merge/write or corrupt-cleanup operation for one
   * composite page key. This is intentionally narrower than session
   * invalidation barriers: unrelated pages remain fully concurrent. */
  private async acquirePageAccess(key: string): Promise<() => void> {
    const previous = this.pageWriteQueues.get(key);
    let release!: () => void;
    const current = new Promise<void>((resolve) => {
      release = resolve;
    });
    this.pageWriteQueues.set(key, current);
    await previous?.catch(() => undefined);
    return () => {
      release();
      if (this.pageWriteQueues.get(key) === current) {
        this.pageWriteQueues.delete(key);
      }
    };
  }

  private beginActiveWrite(scope: HistoryPageCacheScope): () => void {
    const sessionKey = historyPageCacheSessionKey(scope);
    let finish!: () => void;
    const task = new Promise<void>((resolve) => {
      finish = resolve;
    });
    const tasks = this.activeWrites.get(sessionKey) ?? new Set<Promise<void>>();
    tasks.add(task);
    this.activeWrites.set(sessionKey, tasks);
    return () => {
      finish();
      tasks.delete(task);
      if (tasks.size === 0 && this.activeWrites.get(sessionKey) === tasks) {
        this.activeWrites.delete(sessionKey);
      }
    };
  }

  private async waitForSessionWrites(sessionKey: string): Promise<void> {
    const tasks = this.activeWrites.get(sessionKey);
    if (tasks?.size) await Promise.all([...tasks]);
  }

  private async waitForAllWrites(): Promise<void> {
    const tasks = [...this.activeWrites.values()].flatMap(
      (sessionTasks) => [...sessionTasks],
    );
    if (tasks.length) await Promise.all(tasks);
  }

  private record(
    scope: HistoryPageCacheScope,
    page: HistoryBrowsePage,
  ): HistoryPageCacheStoredRecord {
    const key = this.pageKey(scope, page.pageKey);
    const scopeKey = historyPageCacheScopeKey(scope);
    const sessionKey = historyPageCacheSessionKey(scope);
    const base = {
      version: RECORD_VERSION as typeof RECORD_VERSION,
      key,
      scopeKey,
      sessionKey,
      ...scope,
      pageKey: page.pageKey,
      page,
      savedAt: this.now(),
    };
    return { ...base, byteSize: encodedBytes(base) };
  }

  async getPage(
    scope: HistoryPageCacheScope,
    pageKey: string,
  ): Promise<HistoryBrowsePage | null> {
    if (!this.storage || !validScope(scope) || !scope.revision || !pageKey) {
      return null;
    }
    const epoch = this.snapshot(scope);
    if (!await this.waitForBarriers(scope)
        || !this.snapshotMatches(scope, epoch)) {
      return null;
    }
    const key = this.pageKey(scope, pageKey);
    const releasePageAccess = await this.acquirePageAccess(key);
    const expected = {
      key,
      scopeKey: historyPageCacheScopeKey(scope),
      sessionKey: historyPageCacheSessionKey(scope),
      pageKey,
      engine: scope.engine,
    };
    try {
      if (!this.snapshotMatches(scope, epoch)
          || this.invalidationFailed(scope)) {
        return null;
      }
      const value = await this.storage.read(key);
      if (!this.snapshotMatches(scope, epoch)
          || this.invalidationFailed(scope)) {
        return null;
      }
      if (value == null) return null;
      if (!validRecord(value, expected)) {
        await this.storage.deleteKey(key).catch(() => undefined);
        return null;
      }
      return sanitizeHistoryPageForCache(value.page);
    } catch {
      return null;
    } finally {
      releasePageAccess();
    }
  }

  async putPage(
    scope: HistoryPageCacheScope,
    page: HistoryBrowsePage,
  ): Promise<HistoryPageCacheResult> {
    if (!this.storage) return { ok: false, reason: "unavailable" };
    if (!validScope(scope) || !scope.revision || !validPage(page)) {
      return { ok: false, reason: "corrupt" };
    }
    const epoch = this.snapshot(scope);
    if (!await this.waitForBarriers(scope)
        || !this.snapshotMatches(scope, epoch)) {
      return { ok: false, reason: "invalidated" };
    }
    const key = this.pageKey(scope, page.pageKey);
    const releasePageWrite = await this.acquirePageAccess(key);
    if (!this.snapshotMatches(scope, epoch)
        || this.invalidationFailed(scope)) {
      releasePageWrite();
      return { ok: false, reason: "invalidated" };
    }
    const finishWrite = this.beginActiveWrite(scope);
    const expected = {
      key,
      scopeKey: historyPageCacheScopeKey(scope),
      sessionKey: historyPageCacheSessionKey(scope),
      pageKey: page.pageKey,
      engine: scope.engine,
    };
    try {
      const previousValue = await this.storage.read(key);
      if (!this.snapshotMatches(scope, epoch)
          || this.invalidationFailed(scope)) {
        return { ok: false, reason: "invalidated" };
      }
      const previous = validRecord(previousValue, expected)
        ? sanitizeHistoryPageForCache(previousValue.page)
        : null;
      const sanitized = sanitizeHistoryPageForCache(page);
      const record = this.record(scope, mergePages(previous, sanitized));
      if (record.byteSize > this.maxBytes) {
        return { ok: false, reason: "quota" };
      }
      const result = await this.storage.write(record, this.maxBytes);
      if (!this.snapshotMatches(scope, epoch)
          || this.invalidationFailed(scope)) {
        return { ok: false, reason: "invalidated" };
      }
      return { ok: true, evicted: result.evicted };
    } catch (error) {
      return { ok: false, reason: errorReason(error) };
    } finally {
      finishWrite();
      releasePageWrite();
    }
  }

  async invalidateScope(
    scope: HistoryPageCacheSessionScope,
  ): Promise<HistoryPageCacheResult> {
    if (!validScope(scope)) return { ok: false, reason: "corrupt" };
    const key = historyPageCacheSessionKey(scope);
    const previousInvalidation = this.sessionInvalidations.get(key);
    const epoch = (this.sessionEpochs.get(key) ?? 0) + 1;
    this.sessionEpochs.set(key, epoch);
    const task = (async () => {
      try {
        await previousInvalidation?.catch(() => undefined);
        await this.clearTask?.catch(() => undefined);
        await this.waitForSessionWrites(key);
        await this.storage?.deleteSession(key);
        if (this.sessionEpochs.get(key) === epoch) {
          this.failedSessionInvalidations.delete(key);
          for (const [revisionKey, revisionSessionKey] of
            this.failedRevisionInvalidations) {
            if (revisionSessionKey === key) {
              this.failedRevisionInvalidations.delete(revisionKey);
            }
          }
        }
      } catch (error) {
        if (this.sessionEpochs.get(key) === epoch) {
          this.failedSessionInvalidations.add(key);
        }
        throw error;
      }
    })();
    this.sessionInvalidations.set(key, task);
    try {
      await task;
      return { ok: true };
    } catch (error) {
      return { ok: false, reason: errorReason(error) };
    } finally {
      if (this.sessionInvalidations.get(key) === task) {
        this.sessionInvalidations.delete(key);
      }
    }
  }

  async deleteRevision(
    scope: HistoryPageCacheScope,
  ): Promise<HistoryPageCacheResult> {
    if (!validScope(scope) || !scope.revision) {
      return { ok: false, reason: "corrupt" };
    }
    const key = historyPageCacheScopeKey(scope);
    const sessionKey = historyPageCacheSessionKey(scope);
    const previousInvalidation = this.revisionInvalidations.get(key);
    const epoch = (this.revisionEpochs.get(key) ?? 0) + 1;
    this.revisionEpochs.set(key, epoch);
    const task = (async () => {
      try {
        await previousInvalidation?.catch(() => undefined);
        await this.clearTask?.catch(() => undefined);
        await this.waitForSessionWrites(sessionKey);
        await this.storage?.deleteRevision(key);
        if (this.revisionEpochs.get(key) === epoch) {
          this.failedRevisionInvalidations.delete(key);
        }
      } catch (error) {
        if (this.revisionEpochs.get(key) === epoch) {
          this.failedRevisionInvalidations.set(key, sessionKey);
        }
        throw error;
      }
    })();
    this.revisionInvalidations.set(key, task);
    try {
      await task;
      return { ok: true };
    } catch (error) {
      return { ok: false, reason: errorReason(error) };
    } finally {
      if (this.revisionInvalidations.get(key) === task) {
        this.revisionInvalidations.delete(key);
      }
    }
  }

  async clear(): Promise<HistoryPageCacheResult> {
    const epoch = this.globalEpoch + 1;
    this.globalEpoch = epoch;
    const previousClear = this.clearTask;
    const task = (async () => {
      try {
        await previousClear?.catch(() => undefined);
        await this.waitForAllWrites();
        await this.storage?.clear();
        if (this.globalEpoch === epoch) {
          this.globalInvalidationFailed = false;
          this.failedSessionInvalidations.clear();
          this.failedRevisionInvalidations.clear();
        }
      } catch (error) {
        if (this.globalEpoch === epoch) this.globalInvalidationFailed = true;
        throw error;
      }
    })();
    this.clearTask = task;
    try {
      await task;
      return { ok: true };
    } catch (error) {
      return { ok: false, reason: errorReason(error) };
    } finally {
      if (this.clearTask === task) this.clearTask = null;
    }
  }
}
