import type { Engine, SessionInfo, Space, State } from "./protocol";

/** Merge catalog activity with the resident runtime without losing native turns. */
export function mergeSessionActivityState(
  catalogState: State | null | undefined,
  runtimeState: State | null | undefined,
  mirroredRunning = false,
): State | undefined {
  if (runtimeState == null) return catalogState ?? undefined;
  if (runtimeState === "idle"
      && (mirroredRunning || catalogState === "running")) {
    return "running";
  }
  return runtimeState;
}

export function sessionActivityTime(value?: string | null): number {
  if (!value) return Number.NEGATIVE_INFINITY;
  const numeric = Number(value);
  if (Number.isFinite(numeric)) {
    return numeric > 10_000_000_000 ? numeric : numeric * 1000;
  }
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : Number.NEGATIVE_INFINITY;
}

export function compareSessionsByActivity(a: SessionInfo, b: SessionInfo): number {
  const aTime = sessionActivityTime(a.last_modified);
  const bTime = sessionActivityTime(b.last_modified);
  if (aTime !== bTime) return bTime - aTime;
  return a.session_id.localeCompare(b.session_id);
}

/** Select the session a surface should paint while its catalog refreshes.
 * Exact user focus wins even for an archived row; otherwise use the newest
 * visible session, matching the authoritative post-refresh focus policy. */
export function selectSurfaceSession(
  sessions: SessionInfo[], rememberedId?: string | null,
): SessionInfo | null {
  const remembered = rememberedId
    ? sessions.find((session) => session.session_id === rememberedId)
    : undefined;
  return remembered
    ?? [...sessions]
      .filter((session) => session.tag !== "archived")
      .sort(compareSessionsByActivity)[0]
    ?? sessions[0]
    ?? null;
}

export function bumpSessionActivity(
  sessions: SessionInfo[], sessionId: string, activityMs: number,
): SessionInfo[] {
  let changed = false;
  const updated = sessions.map((session) => {
    if (session.session_id !== sessionId) return session;
    const current = sessionActivityTime(session.last_modified);
    if (current >= activityMs) return session;
    changed = true;
    return { ...session, last_modified: String(activityMs) };
  });
  return changed ? updated : sessions;
}

export function setSessionPinned(
  sessions: SessionInfo[], sessionId: string, pinned: boolean,
): SessionInfo[] {
  let changed = false;
  const updated = sessions.map((session) => {
    if (session.session_id !== sessionId || !!session.pinned === pinned) return session;
    changed = true;
    return { ...session, pinned };
  });
  return changed ? updated : sessions;
}

export function sessionCommandTarget(
  session: SessionInfo, fallbackEngine: Engine, fallbackSpace: Space,
): { engine: Engine; space: Space } {
  return {
    engine: session.engine === "codex" || session.engine === "claude" || session.engine === "dsh" || session.engine === "antigravity"
      ? session.engine : fallbackEngine,
    space: session.space === "work" || session.space === "code"
      ? session.space : fallbackSpace,
  };
}

export function visibleDirectorySessions(
  sessions: SessionInfo[], expanded: boolean, filtering: boolean, limit = 5,
): SessionInfo[] {
  return expanded || filtering || sessions.length <= limit
    ? sessions : sessions.slice(0, limit);
}

export function orderCodeDirectoryGroups(
  groups: Record<string, SessionInfo[]>,
): string[] {
  return Object.keys(groups).sort((a, b) => {
    const aTime = groups[a].length > 0
      ? sessionActivityTime(groups[a][0].last_modified)
      : Number.NEGATIVE_INFINITY;
    const bTime = groups[b].length > 0
      ? sessionActivityTime(groups[b][0].last_modified)
      : Number.NEGATIVE_INFINITY;
    if (aTime !== bTime) return bTime - aTime;
    return a.localeCompare(b);
  });
}
