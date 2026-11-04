import type { AskUser } from "./protocol.ts";
import type { NotificationMode } from "./notification-mode.ts";
import type { TurnNotificationPresentation } from "./turn-notification.ts";

/** Only live asks carry context. Replayed cards and Hello seeds stay silent. */
export class QuestionNotificationGate {
  private readonly seen = new Set<string>();

  take(machineId: string, message: AskUser, view: {
    sid: string | null; engine: string; space: string; hidden: boolean;
  }): boolean {
    const context = message.notification_context;
    if (!context || !message.sid) return false;
    const key = JSON.stringify([machineId, message.sid, message.ask_id]);
    if (this.seen.has(key)) return false;
    this.seen.add(key);
    if (this.seen.size > 256) this.seen.delete(this.seen.values().next().value!);
    return view.hidden || message.sid !== view.sid
      || context.engine !== view.engine || context.space !== view.space;
  }
}

export function questionNotificationPresentation(
  message: AskUser, mode: NotificationMode,
): TurnNotificationPresentation {
  const context = message.notification_context;
  const sid = context?.parent_session_id ?? message.sid ?? null;
  if (mode !== "session" || !context || !sid) return {
    title: "cc-remote", body: "远程会话正在等待你的回答",
    sessionId: null, engine: null, space: null,
  };
  const label = context.engine === "codex" ? "Codex" : context.engine === "dsh" ? "DSH" : "Claude";
  const title = context.display_name?.slice(0, 320).replace(/\s/gu, " ")
    .replace(/\p{C}/gu, "").replace(/ +/g, " ").trim().slice(0, 120);
  return {
    title: title || `${label} 会话`, body: `${label} 正在等待你的回答`,
    sessionId: sid, engine: context.engine, space: context.space,
  };
}
