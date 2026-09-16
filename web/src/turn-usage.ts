import type { TokenUsage, TurnUsage } from "./protocol";
import type { Turn } from "./domain/conversation";

export type TurnUsageReadings = Record<string, TurnUsage>;

export function rememberTurnUsage(readings: TurnUsageReadings | undefined,
  incoming: TurnUsage): TurnUsageReadings {
  const previous = Object.hasOwn(readings ?? {}, incoming.turn_id)
    ? readings![incoming.turn_id] : undefined;
  if (previous && (incoming.seq ?? 0) < (previous.seq ?? 0)) return readings!;
  // Replacement snapshots, never arithmetic deltas: replay is idempotent.
  const entries = Object.entries(readings ?? {}).filter(([id]) => id !== incoming.turn_id);
  entries.push([incoming.turn_id, incoming]);
  return Object.fromEntries(entries.slice(-32));
}

export function usageForTurn(turn: Turn, readings: TurnUsageReadings | undefined): TokenUsage | undefined {
  if (!readings) return undefined;
  for (const id of [turn.id, turn.historyTurnId, turn.clientMsgId,
    turn.liveTaskId, turn.codexTurnId, turn.forkPointId]) {
    if (id && Object.hasOwn(readings, id)) return readings[id].usage;
  }
  return undefined;
}

export function compactTokens(value: number | null | undefined): string {
  if (value == null || !Number.isSafeInteger(value) || value < 0) return "—";
  const unit = value >= 999_950 ? 1_000_000 : value >= 1_000 ? 1_000 : 1;
  if (unit === 1) return String(value);
  return `${(value / unit).toFixed(1).replace(/\.0$/, "")}${unit === 1_000 ? "k" : "m"}`;
}
