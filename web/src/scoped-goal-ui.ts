import type { ThreadGoal } from "./protocol";

export const GOAL_UI_PREFERENCES_KEY = "cc-remote-goal-ui-v1";
const MAX_GOAL_UI_PREFERENCES = 256;

export interface GoalUiPreference {
  known: true;
  state?: "present" | "none";
  complete?: boolean;
  hiddenGoal?: string;
  seenAt: number;
}

export type GoalUiPreferences = Record<string, GoalUiPreference>;

export interface GoalEventOwnership {
  machineId: string;
  engine: "claude" | "codex";
  space: "code" | "work";
}

/** Drop both halves of legacy Goal-dismiss migration tracking as one action.
 * A reconnect replays the old reliable command before requesting fresh Goal
 * state; the fresh state must be free to enqueue a new idempotent migration. */
export function resetGoalDismissMigrationTracking(
  migrations: Set<string>,
  migrationByRequest: Map<string, string>,
): void {
  migrations.clear();
  migrationByRequest.clear();
}

export function goalUiScopeKey(
  machineId: string,
  space: "code" | "work",
  engine: "claude" | "codex",
  sid: string,
): string {
  return JSON.stringify([machineId, space, engine, sid]);
}

export function goalUiScopeForEvent(
  sid: string,
  ownership?: GoalEventOwnership,
): string | null {
  if (!ownership) return null;
  return goalUiScopeKey(
    ownership.machineId,
    ownership.space,
    ownership.engine,
    sid,
  );
}

export function goalStableIdentity(goal: ThreadGoal | null | undefined): string | null {
  if (!goal) return null;
  const marker = goal.createdAt ?? goal.setAt;
  if (typeof marker !== "number" || !Number.isFinite(marker) || marker < 0) {
    return null;
  }
  // Codex currently mutates one thread-level Goal record and can reuse
  // createdAt when the objective is replaced. Keep the objective out of
  // localStorage while still making a dismissal specific to that generation.
  let left = 0x9e3779b9;
  let right = 0x85ebca6b;
  for (let index = 0; index < goal.objective.length; index += 1) {
    const code = goal.objective.charCodeAt(index);
    left = Math.imul(left ^ code, 0x85ebca6b);
    right = Math.imul(right ^ code, 0xc2b2ae35);
  }
  const objectiveFingerprint = [
    (left >>> 0).toString(36),
    (right >>> 0).toString(36),
    goal.objective.length.toString(36),
  ].join(".");
  return JSON.stringify([
    goal.engine, goal.threadId, marker, objectiveFingerprint,
  ]);
}

function boundPreferences(preferences: GoalUiPreferences): GoalUiPreferences {
  const entries = Object.entries(preferences)
    .filter(([, value]) => value?.known === true
      && Number.isFinite(value.seenAt) && value.seenAt >= 0)
    .sort(([, left], [, right]) => right.seenAt - left.seenAt)
    .slice(0, MAX_GOAL_UI_PREFERENCES);
  return Object.fromEntries(entries);
}

export function readGoalUiPreferences(
  storage: Pick<Storage, "getItem">,
): GoalUiPreferences {
  try {
    const raw = storage.getItem(GOAL_UI_PREFERENCES_KEY);
    if (!raw) return {};
    const decoded = JSON.parse(raw) as unknown;
    if (!decoded || typeof decoded !== "object" || Array.isArray(decoded)) {
      return {};
    }
    const preferences: GoalUiPreferences = {};
    for (const [key, candidate] of Object.entries(decoded)) {
      if (!candidate || typeof candidate !== "object" || Array.isArray(candidate)) {
        continue;
      }
      const value = candidate as Partial<GoalUiPreference>;
      if (value.known !== true || typeof value.seenAt !== "number"
          || !Number.isFinite(value.seenAt) || value.seenAt < 0) {
        continue;
      }
      preferences[key] = {
        known: true,
        seenAt: value.seenAt,
        ...(value.state === "present" || value.state === "none"
          ? { state: value.state } : {}),
        ...(value.complete === true ? { complete: true } : {}),
        ...(typeof value.hiddenGoal === "string" && value.hiddenGoal
          ? { hiddenGoal: value.hiddenGoal } : {}),
      };
    }
    return boundPreferences(preferences);
  } catch {
    return {};
  }
}

export function writeGoalUiPreferences(
  storage: Pick<Storage, "setItem">,
  preferences: GoalUiPreferences,
): GoalUiPreferences {
  const bounded = boundPreferences(preferences);
  try {
    storage.setItem(GOAL_UI_PREFERENCES_KEY, JSON.stringify(bounded));
  } catch {
    // Private browsing and managed devices may block storage. The caller keeps
    // the in-memory copy so Goal remains usable for the current page lifetime.
  }
  return bounded;
}

export function rememberGoalUi(
  preferences: GoalUiPreferences,
  key: string,
  now = Date.now(),
): GoalUiPreferences {
  const preference: GoalUiPreference = {
    ...preferences[key], known: true, seenAt: now,
  };
  delete preference.hiddenGoal;
  return boundPreferences({
    ...preferences,
    [key]: preference,
  });
}

export function dismissGoalUi(
  preferences: GoalUiPreferences,
  key: string,
  goal: ThreadGoal | null | undefined,
  now = Date.now(),
): GoalUiPreferences {
  const hiddenGoal = goalStableIdentity(goal);
  return boundPreferences({
    ...preferences,
    [key]: {
      known: true,
      seenAt: now,
      state: goal ? "present" : "none",
      ...(goal?.status === "complete" ? { complete: true } : {}),
      ...(hiddenGoal ? { hiddenGoal } : {}),
    },
  });
}

export function reconcileGoalUiPreference(
  preferences: GoalUiPreferences,
  key: string,
  goal: ThreadGoal | null | undefined,
  now = Date.now(),
  authoritativeDismissed = false,
): { preferences: GoalUiPreferences; revealed: boolean } {
  const current = preferences[key];
  const identity = goalStableIdentity(goal);
  const hidden = authoritativeDismissed
    || (!!identity && current?.hiddenGoal === identity);
  const next = boundPreferences({
    ...preferences,
    [key]: {
      known: true,
      seenAt: now,
      state: goal ? "present" : "none",
      ...(goal?.status === "complete" ? { complete: true } : {}),
      ...(hidden && identity ? { hiddenGoal: identity } : {}),
    },
  });
  return { preferences: next, revealed: !!goal && !hidden };
}

/** Legacy `known` only records a past visit, not a currently recoverable Goal.
 * Refresh unknown/absent/completed Goals silently until native state arrives. */
export function shouldRecoverGoalUi(preference?: GoalUiPreference): boolean {
  return preference?.state === "present"
    && !preference.complete && !preference.hiddenGoal;
}

export function rekeyGoalUiPreference(
  preferences: GoalUiPreferences,
  oldKey: string,
  nextKey: string,
): GoalUiPreferences {
  if (oldKey === nextKey || !preferences[oldKey]) return preferences;
  const next = { ...preferences };
  const previous = next[oldKey];
  delete next[oldKey];
  const existing = next[nextKey];
  next[nextKey] = !existing || previous.seenAt >= existing.seenAt
    ? previous : existing;
  return boundPreferences(next);
}
