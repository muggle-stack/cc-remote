import type {
  Engine,
  EngineCapabilities,
  EngineCapabilityItem,
  Space,
} from "./protocol";

export const SKILL_CATALOG_TTL_MS = 5 * 60_000;
export const MAX_SKILL_CATALOGS = 32;

export interface SkillCatalogCacheEntry {
  items: EngineCapabilityItem[];
  fetchedAt: number;
}

export interface SkillCatalogRequest {
  key: string;
  engine: Engine;
  space: Space;
  cwd: string;
  skillsOnly: boolean;
  claudeProfileId?: string | null;
  codexProfileId?: string | null;
  sid?: string | null;
}

export interface SkillCatalogReadIdentity extends SkillCatalogRequest {
  requestId: string;
}

interface ActiveSkillCatalogRead extends SkillCatalogReadIdentity {
  superseded: boolean;
}

interface SkillCatalogMutation extends SkillCatalogReadIdentity {
  generation: number;
}

export interface SkillCatalogAcceptance {
  request: SkillCatalogRequest;
  source: "read" | "mutation";
  superseded: boolean;
}

export const skillCatalogKey = (
  machineId: string,
  engine: Engine,
  space: Space,
  cwd: string,
  codexProfileId?: string | null,
  claudeProfileId?: string | null,
  sid?: string | null,
): string => [
  machineId,
  engine,
  space,
  engine === "codex"
    ? (codexProfileId || "__default__")
    : engine === "claude"
      ? (claudeProfileId || "__default__")
      : (sid || "__new__"),
  cwd || ".",
].join("\u0000");

export const skillCatalogReadKey = (
  catalogKey: string,
  skillsOnly: boolean,
): string => `${catalogKey}\u0000${skillsOnly ? "skills" : "all"}`;

export const skillCatalogResponseMatches = (
  read: SkillCatalogReadIdentity,
  response: EngineCapabilities,
): boolean => (
  response.request_id === read.requestId
  && response.engine === read.engine
  && response.space === read.space
  // An empty cwd deliberately asks the wrapper for its configured default;
  // the response carries that resolved path, which the browser cannot know
  // before the request. Explicit cwd scopes still require exact equality.
  && (!read.cwd || response.cwd === read.cwd)
  && response.skills_only === read.skillsOnly
  && (read.engine !== "dsh" || response.sid === read.sid)
  && (read.engine !== "codex"
    || (response.codex_profile_id ?? null)
      === (read.codexProfileId ?? null))
  && (read.engine !== "claude"
    || (response.claude_profile_id ?? null)
      === (read.claudeProfileId ?? null))
);

export const skillCatalogMutationResponseMatches = (
  mutation: SkillCatalogReadIdentity,
  response: EngineCapabilities,
): boolean => (
  !response.skills_only
  && skillCatalogResponseMatches(mutation, response)
);

export const skillCatalogRefreshSucceeded = (
  response: EngineCapabilities,
): boolean => !(response.errors ?? []).some((error) => (
  response.skills_only
  || error === "capability discovery failed"
  || error.startsWith("skills:")
));

export const skillCatalogFresh = (
  entry: SkillCatalogCacheEntry | undefined,
  now = Date.now(),
): boolean => !!entry && now - entry.fetchedAt < SKILL_CATALOG_TTL_MS;

export function cacheSkillCatalog(
  current: Record<string, SkillCatalogCacheEntry>,
  key: string,
  items: EngineCapabilityItem[],
  fetchedAt = Date.now(),
): Record<string, SkillCatalogCacheEntry> {
  const next = { ...current, [key]: { items, fetchedAt } };
  const keys = Object.keys(next);
  if (keys.length <= MAX_SKILL_CATALOGS) return next;
  keys.sort((a, b) => next[a].fetchedAt - next[b].fetchedAt);
  for (const oldKey of keys.slice(0, keys.length - MAX_SKILL_CATALOGS)) {
    delete next[oldKey];
  }
  return next;
}

export class SkillCatalogRequestCoordinator {
  private readonly begin: (
    request: SkillCatalogRequest,
  ) => string | null;
  // Keep each response shape bounded to one request while allowing the
  // latency-sensitive Skills lane to bypass slow plugin/app/MCP inventory.
  private readonly active = new Map<boolean, ActiveSkillCatalogRead>();
  private readonly queued = new Map<string, SkillCatalogRequest>();
  private readonly mutations = new Map<string, SkillCatalogMutation>();
  private readonly latestMutationByScope = new Map<string, number>();
  private mutationGeneration = 0;

  constructor(begin: (request: SkillCatalogRequest) => string | null) {
    this.begin = begin;
  }

  request(request: SkillCatalogRequest): boolean {
    const readKey = skillCatalogReadKey(request.key, request.skillsOnly);
    const active = this.active.get(request.skillsOnly);
    if (active) {
      const activeKey = skillCatalogReadKey(
        active.key, active.skillsOnly);
      if (activeKey !== readKey || active.superseded) {
        this.queued.set(readKey, request);
      }
      return false;
    }
    if (this.hasPendingMutation(request.key)) {
      this.queued.set(readKey, request);
      return false;
    }
    return this.beginRead(request);
  }

  trackMutation(
    requestId: string | null | undefined,
    request: SkillCatalogRequest,
  ): boolean {
    if (!requestId) return false;
    const generation = ++this.mutationGeneration;
    this.mutations.set(requestId, {
      ...request,
      requestId,
      skillsOnly: false,
      generation,
    });
    this.latestMutationByScope.set(request.key, generation);
    for (const active of this.active.values()) {
      if (active.key === request.key) active.superseded = true;
    }
    return true;
  }

  accept(response: EngineCapabilities): SkillCatalogAcceptance | null {
    let accepted: SkillCatalogAcceptance | null = null;
    const active = this.active.get(response.skills_only);
    if (active && skillCatalogResponseMatches(active, response)) {
      this.active.delete(response.skills_only);
      accepted = {
        request: active,
        source: "read",
        superseded: active.superseded,
      };
    } else if (response.request_id) {
      const mutation = this.mutations.get(response.request_id);
      if (mutation
          && skillCatalogMutationResponseMatches(mutation, response)) {
        this.mutations.delete(response.request_id);
        accepted = {
          request: mutation,
          source: "mutation",
          superseded:
            this.latestMutationByScope.get(mutation.key)
              !== mutation.generation,
        };
        if (!this.hasPendingMutation(mutation.key)) {
          this.latestMutationByScope.delete(mutation.key);
        }
      }
    }
    if (!accepted) return null;
    this.drain();
    return accepted;
  }

  hasPendingRead(key: string, skillsOnly: boolean): boolean {
    const readKey = skillCatalogReadKey(key, skillsOnly);
    const active = this.active.get(skillsOnly);
    return (
      !!active
        && skillCatalogReadKey(active.key, active.skillsOnly)
          === readKey
    ) || this.queued.has(readKey);
  }

  fail(requestId: string): SkillCatalogRequest | null {
    for (const [lane, read] of this.active) {
      if (read.requestId !== requestId) continue;
      this.active.delete(lane);
      this.drain();
      return read;
    }
    const mutation = this.mutations.get(requestId);
    if (!mutation) return null;
    this.mutations.delete(requestId);
    if (!this.hasPendingMutation(mutation.key)) {
      this.latestMutationByScope.delete(mutation.key);
    }
    this.drain();
    return mutation;
  }

  hasPendingMutation(key: string): boolean {
    for (const mutation of this.mutations.values()) {
      if (mutation.key === key) return true;
    }
    return false;
  }

  resetReads(): void {
    this.active.clear();
    this.queued.clear();
  }

  reset(): void {
    this.resetReads();
    this.mutations.clear();
    this.latestMutationByScope.clear();
  }

  private beginRead(request: SkillCatalogRequest): boolean {
    if (this.active.has(request.skillsOnly)) return false;
    const requestId = this.begin(request);
    if (!requestId) return false;
    this.active.set(request.skillsOnly, {
      ...request,
      requestId,
      superseded: false,
    });
    return true;
  }

  private drain(): void {
    for (const [readKey, request] of this.queued) {
      if (this.active.has(request.skillsOnly)) continue;
      if (this.hasPendingMutation(request.key)) continue;
      this.queued.delete(readKey);
      if (this.beginRead(request)) continue;
      // A failed send has already surfaced a command error. Continue so one
      // bad queued request cannot discard or starve unrelated scopes.
    }
  }
}
