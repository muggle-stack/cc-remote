import type { TurnEnd, TurnResult } from "./protocol";
import type { NotificationMode } from "./notification-mode";

export type TurnNotificationOutcome = "success" | "failed" | "interrupted";

export function classifyTurnNotification(
  result: TurnResult,
): TurnNotificationOutcome {
  const subtype = result.subtype.toLowerCase();
  if (["error_during_execution", "interrupted", "cancelled", "canceled"].includes(subtype)) {
    return "interrupted";
  }
  return result.is_error ? "failed" : "success";
}

export function turnNotificationBody(label: string, result: TurnResult): string {
  switch (classifyTurnNotification(result)) {
    case "interrupted": return `${label} 会话已中断`;
    case "failed": return `${label} 会话执行失败`;
    default: return `${label} 会话已经完成`;
  }
}

export function turnNotificationTag(message: TurnEnd): string {
  const boundary = message.turn_id ?? message.seq ?? message.ts;
  return `turn-${message.sid ?? "unknown"}-${boundary}`;
}

export interface TurnNotificationPresentation {
  title: string;
  body: string;
  sessionId: string | null;
  engine: "claude" | "codex" | "dsh" | null;
  space: "code" | "work" | null;
}

function safeDisplayName(value: string | null | undefined): string | null {
  if (!value) return null;
  const normalized = Array.from(value.slice(0, 320))
    .map((character) => /\s/u.test(character)
      ? " "
      : /\p{C}/u.test(character) ? "" : character)
    .join("")
    .replace(/\s+/gu, " ")
    .trim()
    .slice(0, 120);
  return normalized || null;
}

export function turnNotificationPresentation(
  message: TurnEnd,
  mode: NotificationMode,
): TurnNotificationPresentation {
  const context = message.notification_context;
  const sid = context?.parent_session_id ?? message.sid ?? null;
  if (mode !== "session" || !context || !sid) {
    const outcome = classifyTurnNotification(message.result);
    return {
      title: "cc-remote",
      body: outcome === "interrupted"
        ? "远程会话已中断"
        : outcome === "failed"
          ? "远程会话执行失败"
          : "远程会话已经完成",
      sessionId: null,
      engine: null,
      space: null,
    };
  }
  const label = context.engine === "dsh" ? "DSH" : context.engine === "codex" ? "Codex" : "Claude";
  return {
    title: safeDisplayName(context.display_name) ?? `${label} 会话`,
    body: turnNotificationBody(label, message.result),
    sessionId: sid,
    engine: context.engine,
    space: context.space,
  };
}
