import assert from "node:assert/strict";

import {
  HISTORY_PAGE_CACHE_DB_NAME,
  HistoryPageCache,
  historyPageCacheScopeKey,
  historyPageCacheSessionKey,
  sanitizeHistoryPageForCache,
  type HistoryPageCacheScope,
  type HistoryPageCacheStorage,
  type HistoryPageCacheStoredRecord,
} from "../src/history-page-cache.ts";
import type { HistoryBrowsePage } from "../src/history-browse.ts";
import type { Turn } from "../src/reducer.ts";

function turn(id: string, options: Partial<Turn> = {}): Turn {
  return {
    id,
    prompt: `prompt-${id}`,
    blocks: [],
    done: true,
    ts: Number(id.replace(/\D/g, "")) || undefined,
    ...options,
  };
}

const scope: HistoryPageCacheScope = {
  machineId: "machine-a",
  engine: "codex",
  space: "code",
  sid: "session-a",
  revision: "revision-a",
};

class MemoryStorage implements HistoryPageCacheStorage {
  readonly records = new Map<string, unknown>();
  deletedKeys: string[] = [];
  deletedRevisions: string[] = [];
  deletedSessions: string[] = [];

  async read(key: string): Promise<unknown | null> {
    return this.records.get(key) ?? null;
  }

  async write(
    record: HistoryPageCacheStoredRecord,
    maxBytes: number,
  ): Promise<{ evicted: number }> {
    const previous = this.records.get(record.key) as
      | HistoryPageCacheStoredRecord
      | undefined;
    const records = [...this.records.values()]
      .filter((value): value is HistoryPageCacheStoredRecord =>
        !!value && typeof value === "object"
          && typeof (value as { byteSize?: unknown }).byteSize === "number")
      .filter((value) => value.key !== record.key)
      .sort((left, right) => left.savedAt - right.savedAt);
    let total = records.reduce((sum, value) => sum + value.byteSize, 0)
      + record.byteSize;
    let evicted = 0;
    for (const candidate of records) {
      if (total <= maxBytes) break;
      this.records.delete(candidate.key);
      total -= candidate.byteSize;
      evicted += 1;
    }
    if (total > maxBytes) {
      if (previous) this.records.set(previous.key, previous);
      throw new DOMException("quota", "QuotaExceededError");
    }
    this.records.set(record.key, record);
    return { evicted };
  }

  async deleteKey(key: string): Promise<void> {
    this.deletedKeys.push(key);
    this.records.delete(key);
  }

  async deleteRevision(scopeKey: string): Promise<void> {
    this.deletedRevisions.push(scopeKey);
    for (const [key, value] of this.records) {
      const record = value as Partial<HistoryPageCacheStoredRecord>;
      if (record.scopeKey === scopeKey) this.records.delete(key);
    }
  }

  async deleteSession(sessionKey: string): Promise<void> {
    this.deletedSessions.push(sessionKey);
    for (const [key, value] of this.records) {
      const record = value as Partial<HistoryPageCacheStoredRecord>;
      if (record.sessionKey === sessionKey) this.records.delete(key);
    }
  }

  async clear(): Promise<void> {
    this.records.clear();
  }
}

assert.equal(HISTORY_PAGE_CACHE_DB_NAME, "cc_remote_history_page_cache");
assert.equal(
  historyPageCacheScopeKey(scope),
  JSON.stringify([
    "machine-a", "codex", "code", "session-a", "revision-a",
  ]),
);
assert.equal(
  historyPageCacheSessionKey(scope),
  JSON.stringify(["machine-a", "codex", "code", "session-a"]),
);

const heavyPage: HistoryBrowsePage = {
  pageKey: "page-heavy",
  turns: [turn("1", {
    images: [{ media_type: "image/png", data: "BASE64_IMAGE_SECRET" }],
    imageRefs: [{
      image_id: "image-1",
      media_type: "image/png",
      width: 20,
      height: 10,
      byte_size: 999,
    }],
    files: [{ filename: "report.md", data: "FILE_BODY_SECRET" }],
    detailEventCount: 8,
    detailLoaded: true,
    detailLoading: true,
    blocks: [{
      kind: "tool",
      message_id: "tool-message",
      tool_use_id: "tool-use",
      tool: "shell",
      input: { secret: "TOOL_INPUT_SECRET" },
      output: "TOOL_OUTPUT_SECRET",
      diff: "TOOL_DIFF_SECRET",
      progress: "TOOL_PROGRESS_SECRET",
      result: {
        content: "TOOL_RESULT_SECRET",
        is_error: false,
      },
      done: true,
    }, {
      kind: "process",
      item_id: "process-1",
      processKind: "command",
      phase: "end",
      status: "succeeded",
      title: "safe title",
      detail: "PROCESS_DETAIL_SECRET",
      input: { secret: "PROCESS_INPUT_SECRET" },
      output: "PROCESS_OUTPUT_SECRET",
      diff: "PROCESS_DIFF_SECRET",
      progress: "PROCESS_PROGRESS_SECRET",
      command: "PROCESS_COMMAND_SECRET",
      done: true,
    }, {
      kind: "process",
      item_id: "compaction-1",
      processKind: "compaction",
      phase: "end",
      status: "succeeded",
      turn_id: "native-compaction-turn",
      title: "压缩上下文",
      detail: "COMPACTION_DETAIL_SECRET",
      input: { secret: "COMPACTION_INPUT_SECRET" },
      output: "COMPACTION_OUTPUT_SECRET",
      done: true,
    }, {
      kind: "text",
      message_id: "answer-1",
      channel: "final",
      text: "final answer",
      done: true,
    }],
  })],
  hasOlder: false,
  olderCursor: "1",
  newerPageKey: null,
  hasNewer: false,
  isLatest: true,
};
const sanitized = sanitizeHistoryPageForCache(heavyPage);
const encoded = JSON.stringify(sanitized);
for (const forbidden of [
  "BASE64_IMAGE_SECRET",
  "FILE_BODY_SECRET",
  "TOOL_INPUT_SECRET",
  "TOOL_OUTPUT_SECRET",
  "TOOL_DIFF_SECRET",
  "TOOL_PROGRESS_SECRET",
  "TOOL_RESULT_SECRET",
  "PROCESS_DETAIL_SECRET",
  "PROCESS_INPUT_SECRET",
  "PROCESS_OUTPUT_SECRET",
  "PROCESS_DIFF_SECRET",
  "PROCESS_PROGRESS_SECRET",
  "PROCESS_COMMAND_SECRET",
  "COMPACTION_DETAIL_SECRET",
  "COMPACTION_INPUT_SECRET",
  "COMPACTION_OUTPUT_SECRET",
]) {
  assert.equal(encoded.includes(forbidden), false, forbidden);
}
assert.equal(sanitized.turns[0].images, undefined);
assert.deepEqual(sanitized.turns[0].files, [{
  filename: "report.md",
  data: "",
}]);
assert.equal(sanitized.turns[0].imageRefs?.[0].image_id, "image-1");
assert.equal(sanitized.turns[0].detailEventCount, 8);
assert.equal(sanitized.turns[0].detailLoaded, false);
assert.equal(sanitized.turns[0].detailLoading, false);
assert.equal(sanitized.turns[0].blocks.at(-1)?.kind, "text");
assert.deepEqual(
  sanitized.turns[0].blocks.map((block) => block.kind),
  ["process", "text"],
  "only compaction proof and final text survive the page-cache projection",
);
const cachedCompaction = sanitized.turns[0].blocks[0];
assert.equal(cachedCompaction.kind, "process");
assert.equal(cachedCompaction.kind === "process"
  ? cachedCompaction.processKind : null, "compaction");
assert.equal(cachedCompaction.kind === "process"
  ? cachedCompaction.turn_id : null, "native-compaction-turn");

const fallbackPage = sanitizeHistoryPageForCache({ ...heavyPage, turns: [turn("fallback", {
  fileChangesTurnId: "native-file-turn",
  fileChanges: { revision: "immutable-revision", files: [{
    path: "/repo/file.ts", state: "available", additions: 1, deletions: 1,
  }] },
  blocks: [{ kind: "process", item_id: "fallback", processKind: "model",
    tool: "model_refusal_fallback", phase: "snapshot", status: "succeeded", done: true,
    title: "模型已回退", summary: "claude-one → claude-two · 原模型拒绝响应",
    input: { raw: "PROVIDER_SECRET" }, detail: "REFUSAL_SECRET",
  }],
})] });
assert.equal(fallbackPage.turns[0].blocks.length, 1);
const cachedFallback = fallbackPage.turns[0].blocks[0];
assert.equal(cachedFallback.kind === "process" ? cachedFallback.tool : null, "model_refusal_fallback");
assert.equal(fallbackPage.turns[0].detailEventCount, 0,
  "a lightweight fallback notice must not manufacture a process-details entry");
assert.equal(fallbackPage.turns[0].fileChanges?.revision, "immutable-revision");
assert.equal(fallbackPage.turns[0].fileChangesTurnId, "native-file-turn");
assert.doesNotMatch(JSON.stringify(fallbackPage), /PROVIDER_SECRET|REFUSAL_SECRET/);

const storage = new MemoryStorage();
let now = 100;
const cache = new HistoryPageCache({
  storage,
  maxBytes: 1024 * 1024,
  now: () => now++,
});
assert.equal((await cache.putPage(scope, heavyPage)).ok, true);
const cachedHeavy = await cache.getPage(scope, "page-heavy");
assert.deepEqual(
  cachedHeavy?.turns[0].blocks.map((block) => block.kind),
  ["process", "text"],
  "a cache reload keeps compaction proof without resurrecting command rows",
);
const incompatibleV3Key = cache.pageKey(scope, "page-heavy");
(storage.records.get(incompatibleV3Key) as { version: number }).version = 3;
assert.equal(await cache.getPage(scope, "page-heavy"), null,
  "v3 pages without durable compaction proof are rebuilt after hard refresh");
assert.equal(storage.deletedKeys.includes(incompatibleV3Key), true);
const firstWrite = await cache.putPage(scope, {
  pageKey: "page-1",
  turns: [turn("1"), turn("optimistic-2", {
    historyTurnId: "native-2",
  })],
  hasOlder: true,
  olderCursor: "1",
  newerPageKey: "page-2",
});
assert.equal(firstWrite.ok, true);

// Repeated partial eviction of one pageKey must union, not overwrite, earlier
// rows. The canonical native id keeps the optimistic/native overlap singular.
const mergedWrite = await cache.putPage(scope, {
  pageKey: "page-1",
  turns: [turn("native-2"), turn("3")],
  hasOlder: true,
  olderCursor: "1",
  newerPageKey: "page-2",
});
assert.equal(mergedWrite.ok, true);
const mergedPage = await cache.getPage(scope, "page-1");
assert.deepEqual(mergedPage?.turns.map((item) => item.id), [
  "1", "native-2", "3",
]);

// Concurrent partial writes to one logical page must serialize their
// read/merge/write cycle. Otherwise both reads can observe an empty page and
// the last writer silently drops the other turn.
const concurrentStorage = new MemoryStorage();
const concurrentCache = new HistoryPageCache({
  storage: concurrentStorage,
  maxBytes: 1024 * 1024,
});
const concurrentWrites = await Promise.all([
  concurrentCache.putPage(scope, {
    pageKey: "concurrent",
    turns: [turn("10")],
    hasOlder: true,
    olderCursor: "10",
  }),
  concurrentCache.putPage(scope, {
    pageKey: "concurrent",
    turns: [turn("11")],
    hasOlder: true,
    olderCursor: "10",
  }),
]);
assert.deepEqual(concurrentWrites.map((result) => result.ok), [true, true]);
assert.deepEqual(
  (await concurrentCache.getPage(scope, "concurrent"))?.turns.map(
    (item) => item.id,
  ),
  ["10", "11"],
);

const tinyStorage = new MemoryStorage();
const tinyCache = new HistoryPageCache({
  storage: tinyStorage,
  maxBytes: 300,
});
const tooLarge = await tinyCache.putPage(scope, {
  pageKey: "too-large",
  turns: [turn("large", { prompt: "x".repeat(2_000) })],
  hasOlder: false,
  olderCursor: "large",
});
assert.deepEqual(tooLarge, { ok: false, reason: "quota" });
assert.equal(tinyStorage.records.size, 0);

const throwingStorage: HistoryPageCacheStorage = {
  ...new MemoryStorage(),
  read: async () => null,
  write: async () => {
    throw new DOMException("private quota", "QuotaExceededError");
  },
  deleteKey: async () => undefined,
  deleteRevision: async () => undefined,
  deleteSession: async () => undefined,
  clear: async () => undefined,
};
const quotaCache = new HistoryPageCache({
  storage: throwingStorage,
  maxBytes: 1024 * 1024,
});
assert.deepEqual(await quotaCache.putPage(scope, {
  pageKey: "quota",
  turns: [turn("1")],
  hasOlder: false,
  olderCursor: "1",
}), { ok: false, reason: "quota" });

const corruptStorage = new MemoryStorage();
const corruptCache = new HistoryPageCache({ storage: corruptStorage });
const corruptKey = corruptCache.pageKey(scope, "corrupt");
corruptStorage.records.set(corruptKey, { malformed: true });
assert.equal(await corruptCache.getPage(scope, "corrupt"), null);
assert.deepEqual(corruptStorage.deletedKeys, [corruptKey]);

// v4 preserves compaction identity proof. Every older page must be rebuilt,
// including Codex v1/v2 pages which v3 could otherwise reuse: sanitizing those
// records cannot recover proof that was never stored in the first place.
const legacyCodexStorage = new MemoryStorage();
const legacyCodexCache = new HistoryPageCache({ storage: legacyCodexStorage });
assert.equal((await legacyCodexCache.putPage(scope, {
  pageKey: "legacy-codex-v1",
  turns: [turn("legacy-codex")],
  hasOlder: false,
  olderCursor: "legacy-codex",
})).ok, true);
const legacyCodexKey = legacyCodexCache.pageKey(scope, "legacy-codex-v1");
(legacyCodexStorage.records.get(legacyCodexKey) as { version: number })
  .version = 1;
assert.equal(
  await legacyCodexCache.getPage(scope, "legacy-codex-v1"),
  null,
);
assert.deepEqual(legacyCodexStorage.deletedKeys, [legacyCodexKey]);
assert.equal((await legacyCodexCache.putPage(scope, {
  pageKey: "legacy-codex-v2",
  turns: [turn("legacy-codex-v2")],
  hasOlder: false,
  olderCursor: "legacy-codex-v2",
})).ok, true);
const legacyCodexV2Key = legacyCodexCache.pageKey(scope, "legacy-codex-v2");
(legacyCodexStorage.records.get(legacyCodexV2Key) as { version: number })
  .version = 2;
assert.equal(
  await legacyCodexCache.getPage(scope, "legacy-codex-v2"),
  null,
);
assert.deepEqual(
  legacyCodexStorage.deletedKeys,
  [legacyCodexKey, legacyCodexV2Key],
);
assert.equal((await legacyCodexCache.putPage(scope, {
  pageKey: "legacy-codex-v3",
  turns: [turn("legacy-codex-v3")],
  hasOlder: false,
  olderCursor: "legacy-codex-v3",
})).ok, true);
const legacyCodexV3Key = legacyCodexCache.pageKey(scope, "legacy-codex-v3");
(legacyCodexStorage.records.get(legacyCodexV3Key) as { version: number })
  .version = 3;
assert.equal(
  await legacyCodexCache.getPage(scope, "legacy-codex-v3"),
  null,
);
assert.deepEqual(
  legacyCodexStorage.deletedKeys,
  [legacyCodexKey, legacyCodexV2Key, legacyCodexV3Key],
);

// A source revision can stay unchanged when the backend repairs only its
// summary projection. Reject v6 question-only pages rather than repaint them.
const staleAsyncStorage = new MemoryStorage();
const staleAsyncCache = new HistoryPageCache({ storage: staleAsyncStorage });
const staleAsyncPage: HistoryBrowsePage = {
  pageKey: "async-legacy-answer",
  turns: [turn("async-user", { blocks: [{
    kind: "text", message_id: "question", text: "Question?", done: true,
    channel: "final", delivery: "async", questions: [{ title: "Question?" }],
  }] })],
  hasOlder: false,
  olderCursor: "async-user",
};
assert.equal((await staleAsyncCache.putPage(scope, staleAsyncPage)).ok, true);
const staleAsyncKey = staleAsyncCache.pageKey(scope, staleAsyncPage.pageKey);
(staleAsyncStorage.records.get(staleAsyncKey) as { version: number }).version = 6;
assert.equal(await staleAsyncCache.getPage(scope, staleAsyncPage.pageKey), null);
assert.deepEqual(staleAsyncStorage.deletedKeys, [staleAsyncKey]);
staleAsyncPage.turns[0].blocks.push({
  kind: "text", message_id: "answer", text: "Finished.", done: true, channel: "final",
});
assert.equal((await staleAsyncCache.putPage(scope, staleAsyncPage)).ok, true);
const reopenedAsyncCache = new HistoryPageCache({ storage: staleAsyncStorage });
assert.deepEqual((await reopenedAsyncCache.getPage(scope, staleAsyncPage.pageKey))
  ?.turns[0].blocks.map((block) => block.kind === "text" ? block.text : null),
["Question?", "Finished."]);

const claudeScope = { ...scope, engine: "claude" };
const legacyClaudeStorage = new MemoryStorage();
const legacyClaudeCache = new HistoryPageCache({ storage: legacyClaudeStorage });
assert.equal((await legacyClaudeCache.putPage(claudeScope, {
  pageKey: "legacy-claude-v1",
  turns: [turn("legacy-claude")],
  hasOlder: false,
  olderCursor: "legacy-claude",
})).ok, true);
const legacyClaudeKey = legacyClaudeCache.pageKey(
  claudeScope, "legacy-claude-v1",
);
(legacyClaudeStorage.records.get(legacyClaudeKey) as { version: number })
  .version = 1;
assert.equal(
  await legacyClaudeCache.getPage(claudeScope, "legacy-claude-v1"),
  null,
);
assert.deepEqual(legacyClaudeStorage.deletedKeys, [legacyClaudeKey]);

const legacyClaudeV2Storage = new MemoryStorage();
const legacyClaudeV2Cache = new HistoryPageCache({
  storage: legacyClaudeV2Storage,
});
assert.equal((await legacyClaudeV2Cache.putPage(claudeScope, {
  pageKey: "legacy-claude-v2",
  turns: [turn("legacy-claude-v2")],
  hasOlder: false,
  olderCursor: "legacy-claude-v2",
})).ok, true);
const legacyClaudeV2Key = legacyClaudeV2Cache.pageKey(
  claudeScope, "legacy-claude-v2",
);
(legacyClaudeV2Storage.records.get(legacyClaudeV2Key) as { version: number })
  .version = 2;
assert.equal(
  await legacyClaudeV2Cache.getPage(claudeScope, "legacy-claude-v2"),
  null,
);
assert.deepEqual(legacyClaudeV2Storage.deletedKeys, [legacyClaudeV2Key]);

const legacyMissingAvailabilityStorage = new MemoryStorage();
const legacyMissingAvailabilityCache = new HistoryPageCache({
  storage: legacyMissingAvailabilityStorage,
});
assert.equal((await legacyMissingAvailabilityCache.putPage(scope, {
  pageKey: "legacy-missing-availability",
  turns: [turn("1")],
  hasOlder: true,
  olderCursor: "1",
})).ok, true);
const legacyMissingAvailabilityKey = legacyMissingAvailabilityCache.pageKey(
  scope,
  "legacy-missing-availability",
);
const legacyMissingAvailabilityRecord = legacyMissingAvailabilityStorage.records.get(
  legacyMissingAvailabilityKey,
) as { page: { hasOlder?: boolean } };
delete legacyMissingAvailabilityRecord.page.hasOlder;
assert.equal(
  await legacyMissingAvailabilityCache.getPage(
    scope,
    "legacy-missing-availability",
  ),
  null,
  "a legacy page without explicit availability is a cache miss, not EOF",
);
assert.deepEqual(
  legacyMissingAvailabilityStorage.deletedKeys,
  [legacyMissingAvailabilityKey],
);

// No IndexedDB (SSR, private mode, denied storage) is a supported cache miss.
const unavailable = new HistoryPageCache({ indexedDB: null });
assert.equal(await unavailable.getPage(scope, "missing"), null);
assert.deepEqual(await unavailable.putPage(scope, {
  pageKey: "missing",
  turns: [turn("1")],
  hasOlder: false,
  olderCursor: "1",
}), { ok: false, reason: "unavailable" });
assert.equal((await unavailable.invalidateScope(scope)).ok, true);

class FailingDeleteStorage extends MemoryStorage {
  sessionDeleteFailures = 0;
  revisionDeleteFailures = 0;
  clearFailures = 0;

  override async deleteSession(sessionKey: string): Promise<void> {
    if (this.sessionDeleteFailures > 0) {
      this.sessionDeleteFailures -= 1;
      this.deletedSessions.push(sessionKey);
      throw new Error("deleteSession failed");
    }
    await super.deleteSession(sessionKey);
  }

  override async deleteRevision(scopeKey: string): Promise<void> {
    if (this.revisionDeleteFailures > 0) {
      this.revisionDeleteFailures -= 1;
      this.deletedRevisions.push(scopeKey);
      throw new Error("deleteRevision failed");
    }
    await super.deleteRevision(scopeKey);
  }

  override async clear(): Promise<void> {
    if (this.clearFailures > 0) {
      this.clearFailures -= 1;
      throw new Error("clear failed");
    }
    await super.clear();
  }
}

class DeferredReadStorage extends FailingDeleteStorage {
  private deferNext = false;
  private resolveRead: ((value: unknown | null) => void) | null = null;
  private signalReadStarted: (() => void) | null = null;
  readonly readStarted = new Promise<void>((resolve) => {
    this.signalReadStarted = resolve;
  });
  writes = 0;

  deferNextRead(): void {
    this.deferNext = true;
  }

  override read(key: string): Promise<unknown | null> {
    if (!this.deferNext) return super.read(key);
    this.deferNext = false;
    this.signalReadStarted?.();
    this.signalReadStarted = null;
    return new Promise((resolve) => {
      this.resolveRead = resolve;
    });
  }

  finishRead(value: unknown | null): void {
    this.resolveRead?.(value);
    this.resolveRead = null;
  }

  override async write(
    record: HistoryPageCacheStoredRecord,
    maxBytes: number,
  ): Promise<{ evicted: number }> {
    this.writes += 1;
    return super.write(record, maxBytes);
  }
}

// Epoch invalidation wins over a put whose read/merge phase started earlier.
const deferredStorage = new DeferredReadStorage();
const deferredCache = new HistoryPageCache({
  storage: deferredStorage,
  maxBytes: 1024 * 1024,
});
deferredStorage.deferNextRead();
const staleWrite = deferredCache.putPage(scope, {
  pageKey: "stale",
  turns: [turn("1")],
  hasOlder: false,
  olderCursor: "1",
});
await deferredStorage.readStarted;
const invalidation = deferredCache.invalidateScope(scope);
deferredStorage.finishRead(null);
assert.deepEqual(await staleWrite, { ok: false, reason: "invalidated" });
assert.equal((await invalidation).ok, true);
assert.equal(deferredStorage.writes, 0);
assert.equal(deferredStorage.deletedSessions.length, 1);

class DeferredWriteStorage extends FailingDeleteStorage {
  private deferNext = true;
  private finish: (() => void) | null = null;
  private signalWriteStarted: (() => void) | null = null;
  readonly writeStarted = new Promise<void>((resolve) => {
    this.signalWriteStarted = resolve;
  });

  override async write(
    record: HistoryPageCacheStoredRecord,
    maxBytes: number,
  ): Promise<{ evicted: number }> {
    if (this.deferNext) {
      this.deferNext = false;
      this.signalWriteStarted?.();
      this.signalWriteStarted = null;
      await new Promise<void>((resolve) => {
        this.finish = resolve;
      });
    }
    return super.write(record, maxBytes);
  }

  finishWrite(): void {
    this.finish?.();
    this.finish = null;
  }
}

class DeferredDeleteKeyStorage extends FailingDeleteStorage {
  private finish: (() => void) | null = null;
  private signalDeleteStarted: (() => void) | null = null;
  readonly deleteStarted = new Promise<void>((resolve) => {
    this.signalDeleteStarted = resolve;
  });
  writes = 0;

  override async write(
    record: HistoryPageCacheStoredRecord,
    maxBytes: number,
  ): Promise<{ evicted: number }> {
    this.writes += 1;
    return super.write(record, maxBytes);
  }

  override async deleteKey(key: string): Promise<void> {
    this.signalDeleteStarted?.();
    this.signalDeleteStarted = null;
    await new Promise<void>((resolve) => {
      this.finish = resolve;
    });
    await super.deleteKey(key);
  }

  finishDelete(): void {
    this.finish?.();
    this.finish = null;
  }
}

// A cached-newer read must wait for the same pageKey's write-through. Treating
// "write in progress" as a miss would permanently discard the local newer link.
const pendingReadStorage = new DeferredWriteStorage();
const pendingReadCache = new HistoryPageCache({
  storage: pendingReadStorage,
  maxBytes: 1024 * 1024,
});
const pendingPageWrite = pendingReadCache.putPage(scope, {
  pageKey: "pending-read-page",
  turns: [turn("15")],
  hasOlder: false,
  olderCursor: "15",
});
await pendingReadStorage.writeStarted;
let pendingPageReadSettled = false;
const pendingPageRead = pendingReadCache.getPage(
  scope, "pending-read-page",
).then((page) => {
  pendingPageReadSettled = true;
  return page;
});
await Promise.resolve();
await Promise.resolve();
assert.equal(pendingPageReadSettled, false);
pendingReadStorage.finishWrite();
assert.equal((await pendingPageWrite).ok, true);
assert.equal((await pendingPageRead)?.turns[0]?.id, "15");

// Corrupt cleanup shares that same key queue. A stale get which already read a
// corrupt value cannot delete a valid replacement written concurrently.
const corruptRaceScope = { ...scope, sid: "session-corrupt-race" };
const corruptRaceStorage = new DeferredDeleteKeyStorage();
const corruptRaceCache = new HistoryPageCache({
  storage: corruptRaceStorage,
  maxBytes: 1024 * 1024,
});
assert.equal((await corruptRaceCache.putPage(corruptRaceScope, {
  pageKey: "corrupt-race",
  turns: [turn("16")],
  hasOlder: false,
  olderCursor: "16",
})).ok, true);
const corruptRaceKey = corruptRaceCache.pageKey(
  corruptRaceScope, "corrupt-race",
);
const validBeforeCorruption = corruptRaceStorage.records.get(
  corruptRaceKey,
) as HistoryPageCacheStoredRecord;
const corruptValue = {
  ...validBeforeCorruption,
  page: { ...validBeforeCorruption.page, turns: "corrupt" },
};
corruptRaceStorage.records.set(corruptRaceKey, corruptValue);
const corruptRead = corruptRaceCache.getPage(
  corruptRaceScope, "corrupt-race",
);
await corruptRaceStorage.deleteStarted;
const writesBeforeReplacement = corruptRaceStorage.writes;
let replacementSettled = false;
const validReplacement = corruptRaceCache.putPage(corruptRaceScope, {
  pageKey: "corrupt-race",
  turns: [turn("17")],
  hasOlder: false,
  olderCursor: "17",
}).then((result) => {
  replacementSettled = true;
  return result;
});
await new Promise<void>((resolve) => setTimeout(resolve, 0));
assert.equal(replacementSettled, false);
assert.equal(
  corruptRaceStorage.writes,
  writesBeforeReplacement,
  "the replacement cannot race ahead of corrupt cleanup for the same key",
);
corruptRaceStorage.finishDelete();
assert.equal(await corruptRead, null);
assert.equal((await validReplacement).ok, true);
assert.deepEqual(
  (await corruptRaceCache.getPage(
    corruptRaceScope, "corrupt-race",
  ))?.turns.map((item) => item.id),
  ["17"],
);

// Invalidation must run after an already-started storage write. Deleting first
// and merely noticing the epoch mismatch afterwards leaves a stale record which
// a later getPage would accept under the new epoch.
const deferredWriteStorage = new DeferredWriteStorage();
const deferredWriteCache = new HistoryPageCache({
  storage: deferredWriteStorage,
  maxBytes: 1024 * 1024,
});
const lateStoredPage = deferredWriteCache.putPage(scope, {
  pageKey: "late-stored",
  turns: [turn("1")],
  hasOlder: false,
  olderCursor: "1",
});
await deferredWriteStorage.writeStarted;
const deleteAfterWrite = deferredWriteCache.invalidateScope(scope);
deferredWriteStorage.finishWrite();
assert.deepEqual(await lateStoredPage, { ok: false, reason: "invalidated" });
assert.equal((await deleteAfterWrite).ok, true);
assert.equal(await deferredWriteCache.getPage(scope, "late-stored"), null);
assert.equal(deferredWriteStorage.records.size, 0,
  "write-after-delete cannot repopulate an invalidated session scope");

// A deleteSession failure must leave the whole session fail-closed, including a
// get which was already reading the stale record. Retrying the invalidation is
// the only operation which re-opens the cache scope.
const deferredGetScope = { ...scope, sid: "session-deferred-get" };
const deferredGetStorage = new DeferredReadStorage();
const deferredGetCache = new HistoryPageCache({
  storage: deferredGetStorage,
  maxBytes: 1024 * 1024,
});
assert.equal((await deferredGetCache.putPage(deferredGetScope, {
  pageKey: "stale-read",
  turns: [turn("20")],
  hasOlder: false,
  olderCursor: "20",
})).ok, true);
const staleReadKey = deferredGetCache.pageKey(
  deferredGetScope,
  "stale-read",
);
const staleReadRecord = deferredGetStorage.records.get(staleReadKey) ?? null;
deferredGetStorage.deferNextRead();
const staleRead = deferredGetCache.getPage(deferredGetScope, "stale-read");
await deferredGetStorage.readStarted;
deferredGetStorage.sessionDeleteFailures = 1;
const failedReadInvalidation =
  deferredGetCache.invalidateScope(deferredGetScope);
deferredGetStorage.finishRead(staleReadRecord);
assert.equal(await staleRead, null);
assert.deepEqual(await failedReadInvalidation, {
  ok: false,
  reason: "error",
});
assert.equal(deferredGetStorage.records.size, 1);
assert.equal(
  await deferredGetCache.getPage(deferredGetScope, "stale-read"),
  null,
);
assert.deepEqual(await deferredGetCache.putPage(deferredGetScope, {
  pageKey: "blocked",
  turns: [turn("21")],
  hasOlder: false,
  olderCursor: "21",
}), { ok: false, reason: "invalidated" });
assert.equal(
  (await deferredGetCache.invalidateScope(deferredGetScope)).ok,
  true,
);
assert.equal(deferredGetStorage.records.size, 0);
assert.equal((await deferredGetCache.putPage(deferredGetScope, {
  pageKey: "recovered",
  turns: [turn("22")],
  hasOlder: false,
  olderCursor: "22",
})).ok, true);

// The same fail-closed guarantee applies when deleteSession fails after an
// already-started write. The stale write may remain physically stored, but it
// cannot be observed or extended before a successful deletion retry.
const failedWriteScope = { ...scope, sid: "session-failed-write" };
const failedWriteStorage = new DeferredWriteStorage();
const failedWriteCache = new HistoryPageCache({
  storage: failedWriteStorage,
  maxBytes: 1024 * 1024,
});
const failedLateWrite = failedWriteCache.putPage(failedWriteScope, {
  pageKey: "late-failed-delete",
  turns: [turn("30")],
  hasOlder: false,
  olderCursor: "30",
});
await failedWriteStorage.writeStarted;
failedWriteStorage.sessionDeleteFailures = 1;
const failedDeleteAfterWrite =
  failedWriteCache.invalidateScope(failedWriteScope);
failedWriteStorage.finishWrite();
assert.deepEqual(await failedLateWrite, {
  ok: false,
  reason: "invalidated",
});
assert.deepEqual(await failedDeleteAfterWrite, {
  ok: false,
  reason: "error",
});
assert.equal(failedWriteStorage.records.size, 1);
assert.equal(
  await failedWriteCache.getPage(failedWriteScope, "late-failed-delete"),
  null,
);
assert.deepEqual(await failedWriteCache.putPage(failedWriteScope, {
  pageKey: "blocked",
  turns: [turn("31")],
  hasOlder: false,
  olderCursor: "31",
}), { ok: false, reason: "invalidated" });
assert.equal(
  (await failedWriteCache.invalidateScope(failedWriteScope)).ok,
  true,
);
assert.equal(failedWriteStorage.records.size, 0);
assert.equal((await failedWriteCache.putPage(failedWriteScope, {
  pageKey: "recovered",
  turns: [turn("32")],
  hasOlder: false,
  olderCursor: "32",
})).ok, true);

// A revision failure blocks only that revision. A successful retry recovers it
// without hiding another revision in the same session.
const revisionStorage = new FailingDeleteStorage();
const revisionCache = new HistoryPageCache({
  storage: revisionStorage,
  maxBytes: 1024 * 1024,
});
const failedRevisionScope = { ...scope, sid: "session-revision" };
const otherRevisionScope = {
  ...failedRevisionScope,
  revision: "revision-b",
};
assert.equal((await revisionCache.putPage(failedRevisionScope, {
  pageKey: "revision-a-page",
  turns: [turn("40")],
  hasOlder: false,
  olderCursor: "40",
})).ok, true);
assert.equal((await revisionCache.putPage(otherRevisionScope, {
  pageKey: "revision-b-page",
  turns: [turn("41")],
  hasOlder: false,
  olderCursor: "41",
})).ok, true);
revisionStorage.revisionDeleteFailures = 1;
assert.deepEqual(await revisionCache.deleteRevision(failedRevisionScope), {
  ok: false,
  reason: "error",
});
assert.equal(
  await revisionCache.getPage(failedRevisionScope, "revision-a-page"),
  null,
);
assert.equal(
  (await revisionCache.getPage(otherRevisionScope, "revision-b-page"))
    ?.turns[0]?.id,
  "41",
);
assert.deepEqual(await revisionCache.putPage(failedRevisionScope, {
  pageKey: "blocked",
  turns: [turn("42")],
  hasOlder: false,
  olderCursor: "42",
}), { ok: false, reason: "invalidated" });
assert.equal(
  (await revisionCache.deleteRevision(failedRevisionScope)).ok,
  true,
);
assert.equal((await revisionCache.putPage(failedRevisionScope, {
  pageKey: "revision-recovered",
  turns: [turn("43")],
  hasOlder: false,
  olderCursor: "43",
})).ok, true);

// A failed global clear closes every scope until clear itself succeeds.
const clearStorage = new FailingDeleteStorage();
const clearCache = new HistoryPageCache({
  storage: clearStorage,
  maxBytes: 1024 * 1024,
});
const clearScopeA = { ...scope, sid: "session-clear-a" };
const clearScopeB = { ...scope, sid: "session-clear-b" };
assert.equal((await clearCache.putPage(clearScopeA, {
  pageKey: "clear-a",
  turns: [turn("50")],
  hasOlder: false,
  olderCursor: "50",
})).ok, true);
assert.equal((await clearCache.putPage(clearScopeB, {
  pageKey: "clear-b",
  turns: [turn("51")],
  hasOlder: false,
  olderCursor: "51",
})).ok, true);
clearStorage.clearFailures = 1;
assert.deepEqual(await clearCache.clear(), {
  ok: false,
  reason: "error",
});
assert.equal(await clearCache.getPage(clearScopeA, "clear-a"), null);
assert.equal(await clearCache.getPage(clearScopeB, "clear-b"), null);
assert.deepEqual(await clearCache.putPage(clearScopeA, {
  pageKey: "blocked",
  turns: [turn("52")],
  hasOlder: false,
  olderCursor: "52",
}), { ok: false, reason: "invalidated" });
assert.equal((await clearCache.clear()).ok, true);
assert.equal(clearStorage.records.size, 0);
assert.equal((await clearCache.putPage(clearScopeA, {
  pageKey: "clear-recovered",
  turns: [turn("53")],
  hasOlder: false,
  olderCursor: "53",
})).ok, true);

// Revision deletion and global clear are non-throwing even after prior failures.
assert.equal((await cache.deleteRevision(scope)).ok, true);
assert.equal(storage.deletedRevisions.length, 1);
assert.equal((await cache.clear()).ok, true);
assert.equal(storage.records.size, 0);
