import type { AsyncQuestionSpec } from "./protocol";
import type { QueryAcceptanceResult } from "./outbox";

export interface AsyncQuestionDraft {
  choice: string | null;
  text: string;
  scrollTop: number;
}

export interface AsyncQuestionEntry {
  key: string;
  question: AsyncQuestionSpec;
}

interface QuestionSession {
  drafts: Map<string, { signature: string; value: AsyncQuestionDraft }>;
  retired: Set<string>;
  seen: Set<string>;
  submissions: Map<string, Promise<QueryAcceptanceResult>>;
  activeKey: string | null;
  collapsed: boolean;
}

const STORAGE_KEY = "cc_remote_async_question_retired";
const SESSION_LIMIT = 64;
const QUESTION_LIMIT = 128;

/** Drafts and pending receipts stay in memory across device/session switches.
 * Only opaque retired question identities survive a page reload. */
export class AsyncQuestionStore {
  private readonly sessions = new Map<string, QuestionSession>();

  get(scope: string): QuestionSession {
    let session = this.sessions.get(scope);
    if (session) return session;
    let retired: string[] = [];
    try {
      const saved: unknown = JSON.parse(sessionStorage.getItem(STORAGE_KEY) ?? "{}");
      const keys = saved && typeof saved === "object"
        ? (saved as Record<string, unknown>)[scope] : null;
      if (Array.isArray(keys)) retired = keys.filter((key): key is string =>
        typeof key === "string" && key.length <= 512).slice(-QUESTION_LIMIT);
    } catch { /* Private browsing may disable sessionStorage. */ }
    session = { drafts: new Map(), retired: new Set(retired), seen: new Set(),
      submissions: new Map(), activeKey: null, collapsed: false };
    this.sessions.set(scope, session);
    if (this.sessions.size > SESSION_LIMIT) {
      const oldest = [...this.sessions].find(([key, value]) =>
        key !== scope && value.submissions.size === 0);
      if (oldest) this.sessions.delete(oldest[0]);
    }
    return session;
  }

  draft(scope: string, entry: AsyncQuestionEntry): AsyncQuestionDraft {
    const stored = this.get(scope).drafts.get(entry.key);
    return stored?.signature === JSON.stringify(entry.question) ? stored.value
      : { choice: entry.question.options?.[0] ?? null, text: "", scrollTop: 0 };
  }

  saveDraft(scope: string, entry: AsyncQuestionEntry, value: AsyncQuestionDraft): void {
    const drafts = this.get(scope).drafts;
    drafts.delete(entry.key);
    drafts.set(entry.key, { signature: JSON.stringify(entry.question), value });
    if (drafts.size > QUESTION_LIMIT) drafts.delete(drafts.keys().next().value!);
  }

  retire(scope: string, key: string): void {
    const session = this.get(scope);
    session.retired.add(key);
    session.drafts.delete(key);
    if (session.retired.size > QUESTION_LIMIT) {
      session.retired.delete(session.retired.values().next().value!);
    }
    try {
      const saved: unknown = JSON.parse(sessionStorage.getItem(STORAGE_KEY) ?? "{}");
      const rows: Record<string, string[]> = saved && typeof saved === "object" && !Array.isArray(saved)
        ? saved as Record<string, string[]> : {};
      delete rows[scope];
      rows[scope] = [...session.retired];
      const retained = Object.entries(rows).slice(-SESSION_LIMIT);
      sessionStorage.setItem(STORAGE_KEY, JSON.stringify(Object.fromEntries(retained)));
    } catch { /* In-memory dismissal still works without browser storage. */ }
  }

  get hasDrafts(): boolean {
    return [...this.sessions.values()].some(session =>
      [...session.drafts.values()].some(({ value }) =>
        Boolean(value.text.trim() || value.choice?.trim())));
  }

  isCurrent(scope: string, session: QuestionSession): boolean {
    return this.sessions.get(scope) === session;
  }

  clear(): void {
    this.sessions.clear();
    try { sessionStorage.removeItem(STORAGE_KEY); } catch { /* Storage is optional. */ }
  }
}
