import type { Block, ProcessBlock, Turn } from "./domain/conversation.ts";
import type { ThreadGoal } from "./protocol.ts";
import { mergeDetailWithLiveTail } from "./history-merge.ts";

export interface TurnPlanProgress {
  turnId: string;
  block: ProcessBlock;
  detailLoading: boolean;
  needsDetail: boolean;
  /** Millisecond start time of the user turn that owns this Plan. */
  turnStartedAt?: number;
  /** False only when a durable snapshot was rebound to a later unrelated turn. */
  ownerMatchesTurn?: boolean;
}

export type TurnPlanProgressSource = "runtime" | "history";

export interface SessionPlanProgressScope {
  machineId: string;
  engine: "claude" | "codex" | "dsh";
  space: "code" | "work";
  sid: string;
}

export interface ScopedTurnPlanProgress extends TurnPlanProgress {
  source: TurnPlanProgressSource;
}

const MAX_SESSION_PLAN_PROGRESS = 128;

function turnOwnsProgress(turn: Turn, turnId: string): boolean {
  return [
    turn.id,
    turn.clientMsgId,
    turn.historyTurnId,
    turn.forkPointId,
  ].some((candidate) => candidate === turnId);
}

function copyProgress(
  progress: TurnPlanProgress,
  source: TurnPlanProgressSource,
): ScopedTurnPlanProgress {
  return {
    ...progress,
    source,
    block: {
      ...progress.block,
      plan: progress.block.plan?.map((entry) => ({ ...entry })),
    },
  };
}

/** Keep the latest structured Plan independently for each visited session.
 *
 * Codex's full turn detail currently omits update_plan. The summary page and
 * live stream still carry the Plan, so retaining one bounded display snapshot
 * prevents a detail read followed by A -> B -> A navigation from losing the
 * session-level control. Entries are discarded after their owning turn is
 * authoritatively absent, when a completed Plan is followed by a new user
 * turn, or when an invalidation explicitly clears the sid.
 */
export class SessionPlanProgressCache {
  private readonly entries = new Map<string, ScopedTurnPlanProgress>();
  private readonly maxEntries: number;

  constructor(maxEntries = MAX_SESSION_PLAN_PROGRESS) {
    this.maxEntries = maxEntries;
  }

  resolve({
    machineId = "legacy",
    engine = "codex",
    space = "code",
    sid,
    runtime,
    history,
    runtimeTurns,
    historyTurns,
    recovering,
    runtimeLoading,
  }: {
    machineId?: string;
    engine?: "claude" | "codex" | "dsh";
    space?: "code" | "work";
    sid: string;
    runtime: TurnPlanProgress | null;
    history: TurnPlanProgress | null;
    runtimeTurns: readonly Turn[];
    historyTurns: readonly Turn[];
    recovering: boolean;
    runtimeLoading: boolean;
  }): ScopedTurnPlanProgress | null {
    const cacheKey = this.key({ machineId, engine, space, sid });
    const selected = runtime ?? history;
    if (selected) {
      const selectedTurns = runtime ? runtimeTurns : historyTurns;
      const newerTurns = runtime ? [] : runtimeTurns;
      if (terminalPlanHasNewerTurn(
        selected, selectedTurns, newerTurns)) {
        this.entries.delete(cacheKey);
        return null;
      }
      const entry = copyProgress(
        selected, runtime ? "runtime" : "history");
      this.entries.delete(cacheKey);
      this.entries.set(cacheKey, entry);
      while (this.entries.size > this.maxEntries) {
        const oldest = this.entries.keys().next().value;
        if (typeof oldest !== "string") break;
        this.entries.delete(oldest);
      }
      return entry;
    }

    const retained = this.entries.get(cacheKey);
    if (!retained) return null;
    const turns = retained.source === "history" ? historyTurns : runtimeTurns;
    const owner = turns.find((turn) => turnOwnsProgress(
      turn, retained.turnId));
    const newerTurns = retained.source === "history" ? runtimeTurns : [];
    if (owner && terminalPlanHasNewerTurn(
      retained, turns, newerTurns)) {
      this.entries.delete(cacheKey);
      return null;
    }
    if (!owner && !recovering && !runtimeLoading) {
      this.entries.delete(cacheKey);
      return null;
    }
    // Touch on focus so the bounded cache evicts genuinely old sessions first.
    this.entries.delete(cacheKey);
    const resolved = {
      ...retained,
      detailLoading: owner?.detailLoading === true,
      needsDetail: owner
        ? !owner.detailLoaded && (owner.detailEventCount ?? 0) > 0
        : retained.needsDetail,
    };
    this.entries.set(cacheKey, resolved);
    return resolved;
  }

  clear(scope: SessionPlanProgressScope | string): void {
    this.entries.delete(typeof scope === "string"
      ? this.key({
        machineId: "legacy", engine: "codex", space: "code", sid: scope,
      })
      : this.key(scope));
  }

  rekey(
    scope: Omit<SessionPlanProgressScope, "sid"> | string,
    oldSid: string,
    sid?: string,
  ): void {
    const owner = typeof scope === "string"
      ? { machineId: "legacy", engine: "codex" as const, space: "code" as const }
      : scope;
    const oldSessionId = typeof scope === "string" ? scope : oldSid;
    const sessionId = typeof scope === "string" ? oldSid : sid;
    if (!sessionId) return;
    if (oldSessionId === sessionId) return;
    const oldKey = this.key({ ...owner, sid: oldSessionId });
    const key = this.key({ ...owner, sid: sessionId });
    const retained = this.entries.get(oldKey);
    if (!retained) return;
    this.entries.delete(oldKey);
    this.entries.delete(key);
    this.entries.set(key, retained);
  }

  reset(): void {
    this.entries.clear();
  }

  private key(scope: SessionPlanProgressScope): string {
    return [scope.machineId, scope.engine, scope.space, scope.sid].join("\0");
  }
}

/** Resolve the newest plan-bearing turn in a conversation projection.
 *
 * Codex records a task plan on the turn which created it, while later steer or
 * follow-up turns can continue an unfinished task without repeating that plan.
 * Search turns newest-first so old sessions keep their latest active monitor,
 * but retire a completed or negatively terminated Plan as soon as the next
 * user turn begins.
 */
export function latestPlanProgress(
  turns: readonly Turn[],
): TurnPlanProgress | null {
  for (let index = turns.length - 1; index >= 0; index--) {
    const progress = turnPlanProgress(turns[index]);
    if (!progress) continue;
    return terminalPlanHasNewerTurn(progress, turns) ? null : progress;
  }
  return null;
}

function completedGoalAtMs(
  goal: ThreadGoal | null | undefined,
): number | null {
  if (goal?.status !== "complete"
      || typeof goal.updatedAt !== "number"
      || !Number.isFinite(goal.updatedAt)) return null;
  // The Goal API uses epoch seconds; conversation Turn timestamps use epoch
  // milliseconds throughout the reducer and IndexedDB projection.
  return goal.updatedAt * 1000;
}

function turnHasUserContent(turn: Turn): boolean {
  return turn.prompt.trim().length > 0
    || (turn.images?.length ?? 0) > 0
    || (turn.imageRefs?.length ?? 0) > 0
    || (turn.files?.length ?? 0) > 0;
}

/** A completed Goal retires from passive presentation on the next user turn. */
export function completedGoalHasNewerUserTurn(
  goal: ThreadGoal | null | undefined,
  turns: readonly Turn[],
): boolean {
  const completedAt = completedGoalAtMs(goal);
  if (completedAt == null) return false;
  return turns.some((turn) => turnHasUserContent(turn)
    && typeof turn.ts === "number" && Number.isFinite(turn.ts)
    && turn.ts > completedAt);
}

/** Only a Plan from after the completion boundary may outlive the old Goal. */
export function planFollowsCompletedGoal(
  goal: ThreadGoal | null | undefined,
  progress: TurnPlanProgress,
): boolean {
  const completedAt = completedGoalAtMs(goal);
  return completedAt != null
    && typeof progress.turnStartedAt === "number"
    && Number.isFinite(progress.turnStartedAt)
    && progress.turnStartedAt > completedAt
    && progress.ownerMatchesTurn !== false;
}

function terminalPlanHasNewerTurn(
  progress: TurnPlanProgress,
  turns: readonly Turn[],
  newerTurns: readonly Turn[] = [],
): boolean {
  const presentation = planProgressPresentation(progress.block);
  // An active unfinished Plan may span clarification turns. Once its owning
  // turn has actually ended, though, the stale inProgress marker is an audit
  // snapshot rather than active work. Keep it visible until acknowledged by
  // the next user message, then wait for that turn's own Plan.
  if (!progress.block.done
      && !presentation.complete && !presentation.failed) return false;
  const ownerIndex = turns.findIndex((turn) =>
    turnOwnsProgress(turn, progress.turnId));
  if (ownerIndex >= 0
      && turns.slice(ownerIndex + 1).some(turnHasUserContent)) return true;
  if (newerTurns.length === 0) return false;
  const newerOwnerIndex = newerTurns.findIndex((turn) =>
    turnOwnsProgress(turn, progress.turnId));
  if (newerOwnerIndex >= 0) {
    return newerTurns.slice(newerOwnerIndex + 1).some(turnHasUserContent);
  }
  const ownerStartedAt = progress.turnStartedAt
    ?? (ownerIndex >= 0 ? turns[ownerIndex].ts : undefined);
  return newerTurns.some((turn) => !turnOwnsProgress(turn, progress.turnId)
    && turnHasUserContent(turn)
    && (ownerStartedAt == null || turn.ts == null || turn.ts > ownerStartedAt));
}

const PLAN_STEP_RANK = {
  pending: 0,
  inProgress: 1,
  completed: 2,
} as const;

/** Prove that one snapshot advances the same plan without guessing by source.
 *
 * Detail can race the live stream in either direction: a late response may be
 * stale, while a completed detail page may be newer than a retained live tail.
 * Only identical step structure gives us a safe monotonic comparison.
 */
function planSnapshotAdvances(
  candidate: ProcessBlock,
  baseline: ProcessBlock,
): boolean {
  if (!candidate.plan || !baseline.plan
      || candidate.plan.length !== baseline.plan.length) return false;
  let advanced = false;
  for (let index = 0; index < candidate.plan.length; index += 1) {
    const next = candidate.plan[index];
    const previous = baseline.plan[index];
    if (next.step !== previous.step) return false;
    const nextRank = PLAN_STEP_RANK[next.status];
    const previousRank = PLAN_STEP_RANK[previous.status];
    if (nextRank < previousRank) return false;
    if (nextRank > previousRank) advanced = true;
  }
  return advanced;
}

function turnPlanProgress(turn: Turn): TurnPlanProgress | null {
  const preferCompletedDetail = turn.done
    && !turn.detailRestorePending && !turn.detailRestoreIncomplete;
  const plansOnly = (blocks: readonly Block[]) =>
    blocks.filter((block): block is ProcessBlock =>
      block.kind === "process" && block.processKind === "plan");
  const detailPlans = plansOnly(turn.detailProjection?.blocks ?? []);
  const livePlans = plansOnly(mergeDetailWithLiveTail(
    plansOnly(turn.liveSpillBlocks ?? []),
    plansOnly(turn.blocks),
  ));
  const blocks = plansOnly(mergeDetailWithLiveTail(
    detailPlans,
    livePlans,
    preferCompletedDetail,
  ));
  // Match ProcessTimeline: prefer the newest structured update, while still
  // supporting older app-server records which only contain free-form detail.
  const mergedBlock = [...blocks].reverse().find((candidate) =>
    candidate.kind === "process" && candidate.plan != null) ?? blocks.at(-1);
  if (!mergedBlock) return null;
  let block = mergedBlock;
  const detailSnapshot = [...detailPlans].reverse().find((candidate) =>
    candidate.item_id === block.item_id && candidate.plan != null);
  const liveSnapshot = [...livePlans].reverse().find((candidate) =>
    candidate.item_id === block.item_id && candidate.plan != null);
  if (detailSnapshot && liveSnapshot) {
    if (planSnapshotAdvances(liveSnapshot, detailSnapshot)) {
      block = liveSnapshot;
    } else if (planSnapshotAdvances(detailSnapshot, liveSnapshot)) {
      block = detailSnapshot;
    }
  }
  return {
    turnId: turn.id,
    block,
    detailLoading: turn.detailLoading === true,
    needsDetail: !turn.detailLoaded && (turn.detailEventCount ?? 0) > 0,
    turnStartedAt: turn.ts,
    ownerMatchesTurn: !block.turn_id
      || turnOwnsProgress(turn, block.turn_id),
  };
}

export interface PlanProgressPresentation {
  completed: number;
  total: number;
  currentStep: string | null;
  failed: boolean;
  complete: boolean;
  stale: boolean;
  progress: number;
  progressLabel: string;
  stateLabel: string;
  description: string | null;
  fallbackDetail: string | null;
}

export function planProgressPresentation(
  block: ProcessBlock,
  detailLoading = false,
): PlanProgressPresentation {
  const steps = block.plan ?? [];
  const completed = steps.filter((entry) =>
    entry.status === "completed").length;
  const current = steps.find((entry) => entry.status === "inProgress");
  const failed = ["failed", "declined", "cancelled", "interrupted"]
    .includes(block.status);
  // A successful terminal turn does not imply that unfinished structured plan
  // steps ran; step state remains authoritative whenever it exists.
  const complete = !failed && steps.length > 0 && completed === steps.length;
  const stale = block.done && !failed && !complete && steps.length > 0;
  const progress = steps.length > 0
    ? Math.min(100, completed / steps.length * 100)
    : 0;
  return {
    completed,
    total: steps.length,
    currentStep: stale ? null : current?.step ?? null,
    failed,
    complete,
    stale,
    progress,
    progressLabel: steps.length > 0
      ? `${completed} / ${steps.length}`
      : block.done ? "已记录" : "执行中",
    stateLabel: detailLoading ? "正在同步" : failed ? "执行异常"
      : complete ? "全部完成" : stale ? "本轮已结束，计划未更新"
        : block.done ? "执行已结束"
        : current ? "正在执行" : "等待执行",
    description: block.explanation || block.summary || null,
    fallbackDetail: steps.length === 0
      ? block.detail || block.output || block.progress || null
      : null,
  };
}
