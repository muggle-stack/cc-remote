import type { CodexContext, ContextReport } from "./protocol";

const positiveTokens = (value: number | null | undefined): number | null =>
  value != null && Number.isSafeInteger(value) && value > 0 ? value : null;

/** Present observed runtime separately from the native configuration receipt.
 * Matching a usage sample must never clear pending or trigger another reload.
 */
export function codexContextPresentation(
  state: CodexContext | null | undefined,
  report: ContextReport | null | undefined,
) {
  const native = state && report?.source === "native_estimate" && report.available !== false
    && report.model === state.model && !!state.model
    && report.sid === state.sid
    && positiveTokens(report.max_tokens) != null ? report : null;
  const capacity = native?.max_tokens ?? positiveTokens(state?.applied_max_context_tokens);
  // A threshold from an earlier applied window cannot describe a different
  // native window. Missing runtime evidence stays unknown in that case.
  const threshold = positiveTokens(native?.auto_compact_threshold_tokens)
    ?? (!native || native.max_tokens === state?.applied_max_context_tokens
      ? positiveTokens(state?.applied_threshold_tokens) : null);
  const matchesSaved = !!native && state?.max_context_tokens != null
    && native.max_tokens === state.max_context_tokens;
  const saved = state?.max_context_tokens?.toLocaleString() ?? "默认值";
  const status = !state?.pending ? null : matchesSaved
    ? `原生上限 ${capacity!.toLocaleString()}，与已保存设置一致`
    : `已保存上限 ${saved}，${native && state.max_context_tokens != null ? "等待生效" : "待原生运行数据确认"}`;
  return { capacity, threshold, matchesSaved, status };
}

export function parseContextCapacity(value: string): number | null | undefined {
  const text = value.trim().toLowerCase();
  if (["default", "inherit", "auto"].includes(text)) return null;
  const match = /^(\d+(?:\.\d+)?)\s*([km])?$/.exec(text);
  if (!match) return undefined;
  const tokens = Number(match[1]) * (match[2] === "m" ? 1_000_000 : match[2] === "k" ? 1000 : 1);
  return Number.isSafeInteger(tokens) && tokens >= 1 && tokens <= 100_000_000 ? tokens : undefined;
}
