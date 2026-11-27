import type { Engine, Space } from "./protocol";
import { modelsFor, effortsFor, type Catalog, type Effort } from "./data.ts";



export interface NewChatCatalogRequest {
  engine: Engine;
  cwd?: string;
  claudeProfileId?: string;
  codexProfileId?: string;
}

/** Catalog reads are scoped like the session they describe. Work owns its own
 * private cwd, so it must never probe Claude settings through the Code cwd. */
export function newChatCatalogRequest(
  engine: Engine, space: Space, cwd: string,
  codexProfileId?: string | null,
  claudeProfileId?: string | null,
): NewChatCatalogRequest | null {
  if (engine === "dsh") return { engine };
  if (engine === "antigravity") return space === "code" ? { engine, cwd } : null;
  if (engine === "codex") {
    return {
      engine,
      ...(codexProfileId ? { codexProfileId } : {}),
    };
  }
  return {
    engine,
    ...(space === "code" ? { cwd } : {}),
    ...(claudeProfileId ? { claudeProfileId } : {}),
  };
}

export interface NewChatLocalDefaults {
  model: string | null;
  effort: string | null;
}

/** Cwd-aware Claude defaults are presentation metadata for that exact Code
 * directory only. Codex defaults are machine-wide and may be shown in either
 * surface. The selected overrides themselves remain null until the user picks. */
export function resolveNewChatLocalDefaults(
  engine: Engine,
  space: Space,
  cwd: string,
  modelDefaults: Record<string, string>,
  effortDefaults: Record<string, string>,
  defaultCwds: Record<string, string>,
  catalogScopeKey: string = engine,
): NewChatLocalDefaults {
  if ((engine === "claude" || engine === "antigravity")
      && (space !== "code" || defaultCwds[catalogScopeKey] !== cwd)) {
    return { model: null, effort: null };
  }
  return {
    model: modelDefaults[catalogScopeKey] ?? null,
    effort: effortDefaults[catalogScopeKey] ?? null,
  };
}

/** Keep a user's explicit effort only when the newly selected model supports
 * it. Unknown/default targets fail safe to null; we never invent a highest
 * effort on the user's behalf. */
export function compatibleNewChatEffort(
  engine: Engine,
  nextModel: string | null,
  currentEffort: string | null,
  catalog: Catalog,
  localDefaultModel: string | null,
): string | null {
  if (!currentEffort) return null;
  return newChatEfforts(engine, nextModel ?? localDefaultModel, catalog).some(
    (candidate) => candidate.id === currentEffort,
  ) ? currentEffort : null;
}

export function reconcileNewChatSelection(
  engine: Engine,
  model: string | null,
  effort: string | null,
  catalog: Catalog,
  localDefaultModel: string | null,
  modelSource?: "command",
): { model: string | null; effort: string | null } {
  if (model && modelSource !== "command" && !modelsFor(engine, catalog).some(
    (candidate) => candidate.id === model,
  )) {
    // A fallback model can disappear when the authoritative, entitlement-
    // filtered catalog arrives. Clear both overrides instead of submitting a
    // now-inaccessible model with a stale effort.
    return { model: null, effort: null };
  }
  // An explicitly typed /model ID is validated by the native engine. Discovery
  // is not exhaustive, but unknown models still cannot inherit a stale effort.
  return {
    model,
    effort: compatibleNewChatEffort(
      engine, model, effort, catalog, localDefaultModel),
  };
}

export function newChatEfforts(
  engine: Engine,
  effectiveModel: string | null,
  catalog: Catalog,
): Effort[] {
  const model = effectiveModel
    ?? (engine === "claude" ? catalog.claude?.find((entry) => entry.is_default)?.id : null);
  // The picker and reconciliation share this list. Unknown command IDs and
  // unresolved defaults cannot expose fallback levels that validation rejects.
  if (!model || !modelsFor(engine, catalog).some(candidate => candidate.id === model)) return [];
  return effortsFor(engine, model, catalog);
}
