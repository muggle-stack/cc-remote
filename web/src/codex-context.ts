export const CODEX_CONTEXT_USAGE_NOTE =
  "用量为最近一次模型返回值；自动压缩按 Codex 内部上下文估算触发，可能早于页面数字。";

export function parseContextThreshold(value: string): number | null | undefined {
  const text = value.trim().toLowerCase();
  if (["default", "inherit", "auto"].includes(text)) return null;
  const match = /^(\d+(?:\.\d+)?)\s*([km])?$/.exec(text);
  if (!match) return undefined;
  const tokens = Number(match[1]) * (match[2] === "m" ? 1_000_000 : match[2] === "k" ? 1000 : 1);
  return Number.isSafeInteger(tokens) && tokens >= 1 && tokens <= 100_000_000 ? tokens : undefined;
}
