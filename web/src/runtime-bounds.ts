/** Browser-memory bounds for long-lived multi-session tabs.
 *
 * Completed history is recoverable from the authoritative transcript, so it is
 * safe to evict old completed turns and inactive runtimes. Work that has not
 * reached a durable boundary (focused, queued, outbox-targeted, replaying, or a
 * currently-synchronised active turn) is deliberately retained.
 */

export const MAX_RUNTIME_SESSIONS = 64;
export const MAX_RUNTIME_TURNS = 200;
export const MAX_RUNTIME_COMPLETED_UNITS = 16 * 1024 * 1024;

interface SizedImage { media_type?: string; data?: string }
interface SizedFile { filename?: string; data?: string }
interface SizedTextBlock { kind: "text"; message_id?: string; text?: string }
interface SizedToolBlock {
  kind: "tool";
  done?: boolean;
  message_id?: string;
  tool_use_id?: string;
  tool?: string;
  input?: unknown;
  output?: string;
  progress?: string;
  diff?: string;
  result?: { content?: string; summary?: string | null; diff?: string | null };
}
interface SizedProcessBlock {
  kind: "process";
  done?: boolean;
  item_id?: string;
  title?: string;
  summary?: string | null;
  detail?: string | null;
  output?: string | null;
  diff?: string | null;
  progress?: string | null;
  command?: string | null;
  cwd?: string | null;
  input?: unknown;
  plan?: Array<{ step?: string }>;
}

export interface BoundedTurn {
  done: boolean;
  id?: string;
  prompt?: string;
  error?: string;
  progress?: string;
  images?: SizedImage[];
  files?: SizedFile[];
  blocks?: Array<SizedTextBlock | SizedToolBlock | SizedProcessBlock>;
}

function textUnits(value: string | undefined): number {
  return value?.length ?? 0;
}

function turnUnits(turn: BoundedTurn, stopAfter: number): number {
  let units = 256 + textUnits(turn.id) + textUnits(turn.prompt)
    + textUnits(turn.error) + textUnits(turn.progress);
  for (const image of turn.images ?? []) {
    units += 64 + textUnits(image.media_type) + textUnits(image.data);
    if (units > stopAfter) return units;
  }
  for (const file of turn.files ?? []) {
    units += 64 + textUnits(file.filename) + textUnits(file.data);
    if (units > stopAfter) return units;
  }
  for (const block of turn.blocks ?? []) {
    units += 128;
    if (block.kind === "text") {
      units += textUnits(block.message_id) + textUnits(block.text);
    } else if (block.kind === "tool") {
      units += textUnits(block.message_id) + textUnits(block.tool_use_id) + textUnits(block.tool)
        + textUnits(block.output) + textUnits(block.progress) + textUnits(block.diff)
        + textUnits(block.result?.content) + textUnits(block.result?.summary ?? undefined)
        + textUnits(block.result?.diff ?? undefined);
      try { units += JSON.stringify(block.input ?? {}).length; }
      catch { units = stopAfter + 1; }
    } else {
      units += textUnits(block.item_id) + textUnits(block.title)
        + textUnits(block.summary ?? undefined) + textUnits(block.detail ?? undefined)
        + textUnits(block.output ?? undefined) + textUnits(block.diff ?? undefined)
        + textUnits(block.progress ?? undefined) + textUnits(block.command ?? undefined)
        + textUnits(block.cwd ?? undefined);
      for (const entry of block.plan ?? []) units += textUnits(entry.step);
      try { units += JSON.stringify(block.input ?? {}).length; }
      catch { units = stopAfter + 1; }
    }
    if (units > stopAfter) return units;
  }
  return units;
}

function turnHasActiveProcess(turn: {
  blocks?: ReadonlyArray<{ kind?: string; done?: boolean }>;
}): boolean {
  return (turn.blocks ?? []).some((block) =>
    (block.kind === "tool" || block.kind === "process") && block.done === false);
}

/** Keep every in-flight turn and the newest bounded window of completed turns.
 * The newest completed turn is retained even if it alone exceeds the soft byte
 * budget, so completing a large image/tool turn never makes it disappear. */
export function boundRuntimeTurns<T extends BoundedTurn>(
  turns: readonly T[],
  maxTurns = MAX_RUNTIME_TURNS,
  maxCompletedUnits = MAX_RUNTIME_COMPLETED_UNITS,
): T[] {
  if (!turns.length) return turns as T[];
  const activeCount = turns.reduce(
    (count, turn) => count + (!turn.done || turnHasActiveProcess(turn) ? 1 : 0), 0);
  const completedSlots = Math.max(0, maxTurns - activeCount);
  const keep = new Array<boolean>(turns.length).fill(false);
  let completedKept = 0;
  let completedUnits = 0;

  for (let index = turns.length - 1; index >= 0; index--) {
    const turn = turns[index];
    if (!turn.done || turnHasActiveProcess(turn)) {
      keep[index] = true;
      continue;
    }
    if (completedKept >= completedSlots) continue;
    const size = turnUnits(turn, maxCompletedUnits);
    if (completedKept > 0 && completedUnits + size > maxCompletedUnits) continue;
    keep[index] = true;
    completedKept += 1;
    completedUnits += size;
  }

  if (keep.every(Boolean)) return turns as T[];
  return turns.filter((_, index) => keep[index]);
}

export interface RetainedRuntime {
  state: string;
  syncReady: boolean;
  replaying: boolean;
  turns: Array<{
    done: boolean;
    blocks?: Array<{ kind?: string; done?: boolean }>;
  }>;
  queue: unknown[];
  pendingSend: unknown | null;
  pendingQuestion: unknown | null;
}

function hasLiveWork(runtime: RetainedRuntime): boolean {
  if (runtime.queue.length || runtime.pendingSend || runtime.replaying) return true;
  if (!runtime.syncReady) return false;
  return runtime.state !== "idle" || runtime.pendingQuestion !== null
    || runtime.turns.some((turn) => !turn.done || turnHasActiveProcess(turn));
}

/** Oldest-first reclamation by insertion order. The normal idle pool stays at `maxSessions`;
 * explicitly protected/outstanding work may overflow it, but those sources are
 * independently bounded (outbox 256, queued queries 32, wrapper residents 64). */
export function pruneRuntimeMap<T extends RetainedRuntime>(
  runtimes: Record<string, T>,
  explicitlyProtected: ReadonlySet<string>,
  maxSessions = MAX_RUNTIME_SESSIONS,
): Record<string, T> {
  const keys = Object.keys(runtimes);
  if (keys.length <= maxSessions) return runtimes;
  const next = { ...runtimes };
  let remaining = keys.length;

  // Idle/inactive entries are cheapest to reconstruct and go first.
  for (const sid of keys) {
    if (remaining <= maxSessions) break;
    if (explicitlyProtected.has(sid) || hasLiveWork(runtimes[sid])) continue;
    delete next[sid];
    remaining -= 1;
  }
  // A disconnected runtime can retain stale "running" state from an old wrapper
  // generation. If protected entries still force an overflow, discard only these
  // unconfirmed remnants; ReplayStart/Snapshot recreates current residents.
  for (const sid of keys) {
    if (remaining <= maxSessions) break;
    const runtime = runtimes[sid];
    if (!Object.hasOwn(next, sid) || explicitlyProtected.has(sid) || runtime.syncReady
        || runtime.replaying || runtime.queue.length || runtime.pendingSend) continue;
    delete next[sid];
    remaining -= 1;
  }
  return remaining === keys.length ? runtimes : next;
}
