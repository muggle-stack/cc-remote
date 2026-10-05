import type { SessionInfo, SessionMessageReceipt } from "./protocol";
import type { Turn } from "./domain/conversation";

const NATIVE_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;

/** An envelope cannot nominate another machine/account. Resolve only against
 * the current device catalog in the current routing namespace. */
export function resolveRelatedSession(
  nativeId: string, currentSid: string | null, sessions: readonly SessionInfo[],
): SessionInfo | undefined {
  if (!currentSid || !NATIVE_ID.test(nativeId)) return undefined;
  const separator = currentSid.indexOf("@");
  const target = separator < 0 ? nativeId : `${currentSid.slice(0, separator)}@${nativeId}`;
  if (target === currentSid) return undefined;
  return sessions.find((session) => session.session_id === target
    && session.engine === "codex" && session.space !== "work");
}

export function relatedSessionTitle(session: SessionInfo | undefined, nativeId: string): string {
  return session?.summary || session?.first_prompt || nativeId.slice(0, 8);
}

/** Only the native send tool carries an outgoing link, never arbitrary prose. */
export function outgoingSessionMessages(turn: Turn): SessionMessageReceipt[] {
  const receipts = new Map((turn.sessionMessages ?? []).map((r) => [r.itemId, r]));
  for (const block of turn.blocks) {
    if (block.kind !== "tool") continue;
    const native = (block.tool === "send_message_to_thread"
        || block.tool === "codex_app.send_message_to_thread")
      && (block.server === "codex_app" || block.input.namespace === "codex_app");
    if (!native && block.tool !== "mcp__codex_app__send_message_to_thread") continue;
    const target = block.input.threadId;
    if (typeof target !== "string" || !NATIVE_ID.test(target)) continue;
    const result = block.result;
    receipts.set(block.tool_use_id, {
      itemId: block.tool_use_id, threadId: target,
      status: !result ? receipts.get(block.tool_use_id)?.status ?? "sending"
        : result.is_error || ["failed", "cancelled", "declined", "interrupted"].includes(result.status ?? "")
          ? "failed" : "sent",
    });
  }
  return [...receipts.values()].slice(0, 16);
}
