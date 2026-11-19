import {
  MAX_AUTO_COMPACT_TOKENS,
  MIN_AUTO_COMPACT_TOKENS,
  type AutoCompactMode,
} from "./protocol.ts";

export interface AutoCompactSelection {
  mode: AutoCompactMode;
  thresholdTokens: number | null;
}

export const AUTO_COMPACT_PRESETS = [200_000, 500_000, 1_000_000] as const;

export function validAutoCompactThreshold(value: unknown, maximum?: number | null): value is number {
  return typeof value === "number"
    && Number.isSafeInteger(value)
    && value >= MIN_AUTO_COMPACT_TOKENS
    && value <= Math.min(MAX_AUTO_COMPACT_TOKENS, maximum ?? MAX_AUTO_COMPACT_TOKENS);
}

export function normalizeAutoCompactSelection(
  mode: AutoCompactMode,
  thresholdTokens?: number | null,
): AutoCompactSelection {
  if (mode === "custom" && validAutoCompactThreshold(thresholdTokens)) {
    return { mode, thresholdTokens };
  }
  return mode === "auto"
    ? { mode, thresholdTokens: null }
    : { mode: "inherit", thresholdTokens: null };
}

export function formatAutoCompactTokens(value: number): string {
  if (value === 1_000_000) return "1M";
  if (value % 1_000 === 0) return `${value / 1_000}K`;
  return value.toLocaleString();
}

export function autoCompactSelectionLabel(
  selection: AutoCompactSelection,
): string {
  if (selection.mode === "inherit") return "跟随 Claude";
  if (selection.mode === "auto") return "自动";
  return selection.thresholdTokens === null
    ? "自定义"
    : formatAutoCompactTokens(selection.thresholdTokens);
}

export type ParsedAutoCompactArgument =
  | { ok: true; selection: AutoCompactSelection }
  | { ok: false; error: string };

/** Parse the Web-owned slash syntax without forwarding it to the model. */
export function parseAutoCompactArgument(
  argument: string,
  maximum?: number | null,
): ParsedAutoCompactArgument {
  const value = argument.trim().toLowerCase();
  if (value === "inherit") {
    return { ok: true, selection: { mode: "inherit", thresholdTokens: null } };
  }
  if (value === "auto") {
    return { ok: true, selection: { mode: "auto", thresholdTokens: null } };
  }
  const match = /^(\d+(?:\.\d+)?)\s*([km]?)$/.exec(value);
  if (!match) {
    return {
      ok: false,
      error: "用法：/autocompact <inherit | auto | 100k–1m>",
    };
  }
  const numeric = Number(match[1]);
  const scale = match[2] === "m" ? 1_000_000
    : match[2] === "k" ? 1_000 : 1;
  const thresholdTokens = numeric * scale;
  if (!validAutoCompactThreshold(thresholdTokens, maximum)) {
    return {
      ok: false,
      error: maximum != null && thresholdTokens > maximum
        ? `自动压缩阈值不能超过模型最大窗口 ${formatAutoCompactTokens(maximum)}。`
        : "自动压缩阈值需为 100K–1M 的整数 token 数。",
    };
  }
  return {
    ok: true,
    selection: { mode: "custom", thresholdTokens },
  };
}
