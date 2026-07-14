// IndexedDB cache: per-session turns + lastSeq.
//
// Opening the app restores history instantly from local storage and asks the
// wrapper only for the delta (seq > lastSeq) instead of replaying the whole
// buffer — that's what makes other apps feel instant vs. our "一段段补发".
//
// Writes are coalesced (turns change every frame during streaming).

const DB_NAME = "cc_remote_cache";
const STORE = "sessions";
const SCHEMA = 1;

// Bump when the cached turn shape changes in a way old entries can't be
// trusted (e.g. a past bug left tool-only turns without text). Old entries are
// ignored -> client falls back to a full buffer replay (text + tools restored).
// v6 adds assistant channels and structured process blocks. Older caches would
// otherwise render commentary as final text and lose process ordering.
const CACHE_VER = 6;
const MAX_CACHE_SESSIONS = 64;
const MAX_CACHE_TURNS = 100;
const MAX_CACHE_BYTES = 2 * 1024 * 1024;

interface ReplayRecord {
  sid: string;
  lastSeq: number;
  generation?: string;
  savedAt: number;
}

function retainNewest(records: ReplayRecord[], record: ReplayRecord): void {
  records.push(record);
  records.sort((a, b) => b.savedAt - a.savedAt);
  if (records.length > MAX_CACHE_SESSIONS) records.length = MAX_CACHE_SESSIONS;
}

/** Bound the structured clone written for one session. Large image turns are
 * intentionally omitted from the instant-paint cache; authoritative history is
 * fetched on focus, while the replay cursor remains stored separately. */
export function boundCachedTurns(turns: unknown[]): unknown[] {
  const candidates = turns.slice(-MAX_CACHE_TURNS).map((turn) => {
    if (!turn || typeof turn !== "object" || Array.isArray(turn)) return turn;
    const record = turn as Record<string, unknown>;
    if (!Array.isArray(record.files)) return turn;
    // QueryFile.data is needed only until the command is put on the wire.  The
    // transcript protocol intentionally echoes metadata only, so make the
    // best-effort local cache obey the same rule even if an optimistic turn from
    // an older caller still contains the original body.
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
  });
  const kept: unknown[] = [];
  let bytes = 2; // []
  for (let i = candidates.length - 1; i >= 0; i--) {
    let encoded: string | undefined;
    try { encoded = JSON.stringify(candidates[i]); } catch { continue; }
    if (encoded === undefined) continue;
    const size = new TextEncoder().encode(encoded).byteLength + 1;
    if (size > MAX_CACHE_BYTES || bytes + size > MAX_CACHE_BYTES) continue;
    kept.unshift(candidates[i]);
    bytes += size;
  }
  return kept;
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
      if (!value || value.v !== CACHE_VER) {
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
  generation?: string;
  savedAt: number;
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
  if (typeof indexedDB === "undefined") return null;
  try {
    const d = await db();
    return await new Promise<CachedSession | null>((resolve) => {
      const tx = d.transaction(STORE, "readonly");
      const req = tx.objectStore(STORE).get(sessionId);
      req.onsuccess = () => {
        const r = req.result as (CachedSession & { v?: number }) | undefined;
        // Ignore stale caches from before the current shape (v must match).
        resolve(r && r.v === CACHE_VER ? r : null);
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
}> {
  if (typeof indexedDB === "undefined") return { cursors: {}, generations: {} };
  try {
    const d = await db();
    const replay = await new Promise<{
      cursors: Record<string, number>;
      generations: Record<string, string>;
    }>((resolve) => {
      const records: ReplayRecord[] = [];
      const req = d.transaction(STORE, "readonly").objectStore(STORE).openCursor();
      req.onsuccess = () => {
        const cur = req.result;
        if (!cur) {
          const cursors: Record<string, number> = {};
          const generations: Record<string, string> = {};
          for (const record of records) {
            cursors[record.sid] = record.lastSeq;
            if (record.generation) generations[record.sid] = record.generation;
          }
          resolve({ cursors, generations });
          return;
        }
        const r = cur.value as (CachedSession & { v?: number }) | undefined;
        if (r && r.v === CACHE_VER && typeof r.lastSeq === "number" && r.lastSeq > 0) {
          retainNewest(records, {
            sid: String(cur.key),
            lastSeq: r.lastSeq,
            generation: typeof r.generation === "string" && r.generation
              ? r.generation : undefined,
            savedAt: typeof r.savedAt === "number" ? r.savedAt : 0,
          });
        }
        cur.continue();
      };
      req.onerror = () => resolve({ cursors: {}, generations: {} });
    });
    // Queue a bounded cleanup after the readonly cursor transaction. This also
    // repairs databases created by older builds that never evicted sessions.
    void pruneCacheStore(d);
    return replay;
  } catch {
    return { cursors: {}, generations: {} };
  }
}

const pending = new Map<string, {
  sid: string; turns: unknown[]; lastSeq: number; generation?: string;
}>();
let saveTimer: ReturnType<typeof setTimeout> | null = null;

/** Coalesced write — call freely on every turns change; actual IDB write is
 *  debounced 400ms so streaming doesn't hammer IndexedDB. */
export function saveSession(
  sessionId: string, turns: unknown[], lastSeq: number, generation?: string,
): void {
  if (typeof indexedDB === "undefined" || !sessionId) return;
  if (!pending.has(sessionId) && pending.size >= MAX_CACHE_SESSIONS) {
    const oldest = pending.keys().next().value as string | undefined;
    if (oldest) pending.delete(oldest);
  }
  pending.set(sessionId, { sid: sessionId, turns, lastSeq, generation });
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
        const turns = boundCachedTurns(job.turns);
        store.put(
          { v: CACHE_VER, turns, lastSeq: job.lastSeq,
            generation: job.generation, savedAt: Date.now() }, job.sid);
      }
      tx.oncomplete = () => resolve();
      tx.onerror = () => resolve();
    });
    await pruneCacheStore(d);
  } catch { /* ignore — cache is best-effort */ }
  if (pending.size && !saveTimer) saveTimer = setTimeout(flush, 400);
}

/** Explicit logout removes locally cached prompts and base64 images. */
export async function clearCache(): Promise<void> {
  pending.clear();
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
