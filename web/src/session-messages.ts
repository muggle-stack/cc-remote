import type { SessionInfo, SessionMessageReceipt } from "./protocol";

export interface SessionMessageTarget {
  title: string;
  available: boolean;
  account?: string | null;
}

export interface SessionMessageNavigation {
  sessionLink?: (id: string) => SessionMessageTarget;
  onOpenSession?: (id: string) => void;
}

export interface SessionMessageToolContext extends SessionMessageNavigation {
  sessionMessages?: readonly SessionMessageReceipt[] | null;
}

const NATIVE_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const LOCAL_REF = /^ccr:(claude|codex):([A-Za-z0-9][A-Za-z0-9._@-]{0,110})$/;
const GEMINI_REF = /^ccr:antigravity:([A-Za-z0-9][A-Za-z0-9._-]{0,110})$/;

export function localSessionReference(value: string): { engine: string; sid: string } | null {
  const gemini = GEMINI_REF.exec(value);
  if (gemini) return { engine: "antigravity", sid: `agy@${gemini[1]}` };
  const match = LOCAL_REF.exec(value);
  return match && value.length <= 128 ? { engine: match[1], sid: match[2] } : null;
}

/** Native IDs stay within their account; explicit unified refs can identify
 * another engine/account in this device's catalog. Never a different device. */
export function resolveRelatedSession(
  nativeId: string, currentSid: string | null, sessions: readonly SessionInfo[],
): SessionInfo | undefined {
  if (!currentSid) return undefined;
  const local = localSessionReference(nativeId);
  if (local) return sessions.find((session) => session.session_id === local.sid
    && session.engine === local.engine && session.space !== "work" && session.session_id !== currentSid);
  if (!NATIVE_ID.test(nativeId)) return undefined;
  const separator = currentSid.indexOf("@");
  const target = separator < 0 ? nativeId : `${currentSid.slice(0, separator)}@${nativeId}`;
  if (target === currentSid) return undefined;
  return sessions.find((session) => session.session_id === target
    && session.engine === "codex" && session.space !== "work");
}

export function relatedSessionTitle(session: SessionInfo | undefined, nativeId: string): string {
  const title = session?.summary || session?.first_prompt;
  if (title) return title;
  const local = localSessionReference(nativeId);
  if (!local) return nativeId.slice(0, 8);
  const engine = local.engine === "antigravity" ? "Gemini" : local.engine === "claude" ? "Claude" : "Codex";
  return `${engine} · ${local.sid.split("@").at(-1)!.slice(0, 8)}`;
}
