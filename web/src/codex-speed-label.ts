import type { ModelServiceTier } from "./protocol";

export const normalizeSpeed = (tier: string): string =>
  tier === "fast" ? "priority" : tier || "default";

export function speedLabel(tier: string | null | undefined, options: ModelServiceTier[] = []): string {
  if (tier == null) return "速度读取中";
  const id = normalizeSpeed(tier);
  if (id === "default") return "标准";
  if (id === "priority") return "快速";
  if (id === "ultrafast") return "Ultrafast";
  return options.find((option) => option.id === id)?.name ?? id;
}
