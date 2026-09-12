import type { Engine, Space } from "./protocol";
import { modelsFor, effortsFor, type Catalog, type Effort } from "./data";



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
  if (engine === "codex") {
    return {
      engine,
      ...(codexProfileId ? { codexProfileId } : {}),
    };
  }
  return space === "code" ? {
    engine,
    cwd,
    ...(claudeProfileId ? { claudeProfileId } : {}),
  } : null;
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
  if (engine === "claude"
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
  const effectiveModel = nextModel ?? localDefaultModel;
  if (!effectiveModel) return null;
  if (!modelsFor(engine, catalog).some(
    (candidate) => candidate.id === effectiveModel,
  )) return null;
  return effortsFor(engine, effectiveModel, catalog).some(
    (candidate) => candidate.id === currentEffort,
  ) ? currentEffort : null;
}

export function reconcileNewChatSelection(
  engine: Engine,
  model: string | null,
  effort: string | null,
  catalog: Catalog,
  localDefaultModel: string | null,
): { model: string | null; effort: string | null } {
  if (model && !modelsFor(engine, catalog).some(
    (candidate) => candidate.id === model,
  )) {
    // A fallback model can disappear when the authoritative, entitlement-
    // filtered catalog arrives. Clear both overrides instead of submitting a
    // now-inaccessible model with a stale effort.
    return { model: null, effort: null };
  }
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
  // Without an authoritative Codex default there is no model against which an
  // explicit effort can be validated. Keep only the null/default choice.
  if (engine === "codex" && !effectiveModel) return [];
  return effortsFor(engine, effectiveModel, catalog);
}
