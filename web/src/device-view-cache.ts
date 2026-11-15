import type { AppState } from "./reducer";
import type { Engine, SessionInfo, Space } from "./protocol";
import type { Turn } from "./domain/conversation";
import {
  boundCachedTurns, hasOrphanedActiveCompactionProjection, MAX_CACHE_SESSIONS,
} from "./cache";

export interface DeviceView {
  sessions: SessionInfo[];
  sid: string;
  turns: Turn[];
  revision: string | null;
  generation: string | null;
  historyAtStart: boolean;
  metadata: Pick<AppState,
    "claudeProfiles" | "defaultClaudeProfileId" | "claudeProfileByScope"
    | "codexProfiles" | "defaultCodexProfileId" | "codexProfileByScope"
    | "catalog" | "catalogDefault" | "catalogDefaultEffort" | "catalogDefaultCwd">;
}

/** Bounded, paint-only views. Every return still validates its live catalog. */
export class DeviceViewCache {
  private views = new Map<string, {
    machineId: string; engine: Engine; space: Space; view: DeviceView;
  }>();

  remember(machineId: string, engine: Engine, space: Space, state: AppState): void {
    const key = JSON.stringify([machineId, engine, space]);
    const session = state.sessions.find(item => item.session_id === state.focusedSid);
    const runtime = state.runtimes[state.focusedSid ?? ""];
    if (state.newChat || !session || (session.engine ?? "claude") !== engine
        || (session.space ?? "code") !== space || !runtime
        || runtime.historyInvalidated
        || (!runtime.turns.length && !runtime.historyHeadKnown)) {
      this.views.delete(key);
      return;
    }
    const turns = boundCachedTurns(runtime.turns) as Turn[];
    if (hasOrphanedActiveCompactionProjection(turns)) {
      this.views.delete(key);
      return;
    }
    this.views.delete(key);
    this.views.set(key, { machineId, engine, space, view: {
      sessions: [...state.sessions], sid: session.session_id, turns,
      revision: runtime.historyRevision,
      generation: runtime.historyGeneration,
      historyAtStart: runtime.historyHeadKnown && !runtime.hasMore
        && turns.length === runtime.turns.length,
      metadata: {
        claudeProfiles: state.claudeProfiles,
        defaultClaudeProfileId: state.defaultClaudeProfileId,
        claudeProfileByScope: state.claudeProfileByScope,
        codexProfiles: state.codexProfiles,
        defaultCodexProfileId: state.defaultCodexProfileId,
        codexProfileByScope: state.codexProfileByScope,
        catalog: state.catalog, catalogDefault: state.catalogDefault,
        catalogDefaultEffort: state.catalogDefaultEffort,
        catalogDefaultCwd: state.catalogDefaultCwd,
      },
    } });
    while (this.views.size > MAX_CACHE_SESSIONS) {
      this.views.delete(this.views.keys().next().value!);
    }
  }

  get(machineId: string, engine: Engine, space: Space): DeviceView | undefined {
    return this.views.get(JSON.stringify([machineId, engine, space]))?.view;
  }

  sessionLists(machineId: string): Record<string, SessionInfo[]> {
    return Object.fromEntries([...this.views.values()]
      .filter(entry => entry.machineId === machineId)
      .map(entry => [`${entry.space}:${entry.engine}`, entry.view.sessions]));
  }

  acceptSessionList(machineId: string, engine: Engine, space: Space, sessions: SessionInfo[]): void {
    const key = JSON.stringify([machineId, engine, space]);
    const retained = this.views.get(key);
    if (!retained) return;
    if (!sessions.some(session => session.session_id === retained.view.sid)) {
      this.views.delete(key);
      return;
    }
    retained.view = { ...retained.view, sessions: [...sessions] };
  }

  invalidateSession(machineId: string, sid: string): void {
    for (const [key, entry] of this.views) {
      if (entry.machineId === machineId && entry.view.sid === sid) this.views.delete(key);
    }
  }

  retainMachines(machineIds: string[]): void {
    const allowed = new Set(machineIds);
    for (const [key, entry] of this.views) {
      if (!allowed.has(entry.machineId)) this.views.delete(key);
    }
  }

  clear(): void {
    this.views.clear();
  }
}
