import type { AskUser, AssistantMsgEnd } from "./protocol.ts";
import type { NotificationMode } from "./notification-mode.ts";
import type { TurnNotificationPresentation } from "./turn-notification.ts";

type QuestionNotification = AskUser | AssistantMsgEnd;

export function questionNotificationId(message: QuestionNotification): string {
  return message.type === "ask_user" ? message.ask_id : message.message_id;
}

/** Only live asks carry context. Replayed cards and Hello seeds stay silent. */
export class QuestionNotificationGate {
  private readonly seen = new Set<string>();

  take(machineId: string, message: QuestionNotification, view: {
    sid: string | null; engine: string; space: string; hidden: boolean;
  }): boolean {
    const context = message.notification_context;
    if (!context || !message.sid || message.to) return false;
    if (message.type === "assistant_msg_end"
        && (message.delivery !== "async" || !message.questions?.length)) return false;
    const key = JSON.stringify([machineId, message.sid, message.type, questionNotificationId(message)]);
    if (this.seen.has(key)) return false;
    this.seen.add(key);
    if (this.seen.size > 256) this.seen.delete(this.seen.values().next().value!);
    return view.hidden || message.sid !== view.sid
      || context.engine !== view.engine || context.space !== view.space;
  }
}

export function questionNotificationPresentation(
  message: QuestionNotification, mode: NotificationMode,
): TurnNotificationPresentation {
  const context = message.notification_context;
  const sid = context?.parent_session_id ?? message.sid ?? null;
  const asyncQuestion = message.type === "assistant_msg_end";
  if (mode !== "session" || !context || !sid) return {
    title: asyncQuestion ? "Codex 想确认一下" : "cc-remote",
    body: asyncQuestion ? "远程会话有一个问题待回答" : "远程会话正在等待你的回答",
    sessionId: null, engine: null, space: null,
  };
  const label = context.engine === "codex" ? "Codex" : context.engine === "dsh" ? "DSH" : "Claude";
  const title = context.display_name?.slice(0, 320).replace(/\s/gu, " ")
    .replace(/\p{C}/gu, "").replace(/ +/g, " ").trim().slice(0, 120);
  return {
    title: title || `${label} 会话`,
    body: asyncQuestion ? "Codex 想确认一下" : `${label} 正在等待你的回答`,
    sessionId: sid, engine: context.engine, space: context.space,
  };
}
