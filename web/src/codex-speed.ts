import type { Catalog } from "./data";
import type { ModelServiceTier } from "./protocol";

import { normalizeSpeed, speedLabel } from "./codex-speed-label.ts";
export { normalizeSpeed, speedLabel } from "./codex-speed-label.ts";

export function speedsFor(model: string | null | undefined, catalog?: Catalog) {
  const entry = catalog?.codex?.find((candidate) => candidate.id === model);
  const known = Array.isArray(entry?.service_tiers);
  const options: ModelServiceTier[] = [{
    id: "default", name: "标准", description: "使用标准速度",
  }];
  const seen = new Set(["default", "toggle"]);
  for (const option of entry?.service_tiers ?? []) {
    const id = normalizeSpeed(option.id);
    if (!/^[a-z][a-z0-9_-]{0,63}$/.test(id) || seen.has(id)) continue;
    seen.add(id);
    options.push({ ...option, id, name: speedLabel(id, [option]) });
  }
  return { known, options, modelName: entry?.display_name || model || "模型读取中" };
}

/** New-chat choices are model-bound: never revive a stale paid selection. */
export function newChatSpeed(tier: string, model: string | null | undefined, catalog?: Catalog): string {
  const id = normalizeSpeed(tier);
  return speedsFor(model, catalog).options.some((option) => option.id === id)
    ? id : "default";
}
