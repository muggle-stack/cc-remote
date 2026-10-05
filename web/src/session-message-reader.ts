import { createRuntime, initialState, reduce, type AppState } from "./reducer";
import { PROTOCOL_VERSION, type History, type ServerEvent } from "./protocol";

/** A private read-only projection. It never changes the focused chat, starts an
 * engine, or installs a page into the live conversation's state. */
export class SessionMessageReader {
  state: AppState = initialState;
  revision: string | null = null;
  generation: string | null = null;
  page = 0;
  cursors: (string | null)[] = [null];
  hasMore = false;
  oldestId: string | null = null;
  loading = false;
  error: string | null = null;
  private pendingPage: number | null = null;
  private detailRequests = new Map<string, string | null>();

  readonly sid: string;
  constructor(sid: string) { this.sid = sid; }

  get turns() { return this.state.runtimes[this.sid]?.turns ?? []; }

  requestPage(page: number): { before: string | null } | null {
    if (this.loading || page < 0 || page > this.cursors.length - 1) return null;
    this.loading = true;
    this.error = null;
    this.pendingPage = page;
    // A late detail response belongs to the page being left, not the pending
    // page request (and must not settle its timeout).
    this.detailRequests.clear();
    return { before: this.cursors[page] };
  }

  requestDetail(turnId: string, before: string | null = null): boolean {
    if (this.loading || !this.revision || this.detailRequests.size > 0
        || !this.turns.some((turn) => turn.id === turnId)) return false;
    this.detailRequests.set(turnId, before);
    this.state = reduce(this.state, {
      type: "turn_detail_requested", sid: this.sid, turnId, before,
    });
    return true;
  }

  fail(message = "会话读取失败，请重试。"): void {
    this.error = message;
    this.loading = false;
    this.pendingPage = null;
    for (const [turnId, before] of this.detailRequests) {
      this.state = reduce(this.state, { type: "event", event: {
        v: PROTOCOL_VERSION, type: "turn_detail", ts: 0, session_id: this.sid,
        turn_id: turnId, revision: this.revision ?? "", before,
        authoritative: false, error: message, events: [],
      } });
    }
    this.detailRequests.clear();
  }

  accept(event: ServerEvent): boolean {
    if (event.type === "history_invalidated" && event.session_id === this.sid) {
      this.fail("会话历史已更新，请重新读取。");
      this.revision = null;
      this.cursors = [null];
      return true;
    }
    if (event.type === "history" && event.session_id === this.sid) {
      const page = this.pendingPage;
      if (page === null || (event.before ?? null) !== this.cursors[page]) return false;
      if (event.authoritative === false || event.error || !Array.isArray(event.turns)) {
        this.fail(event.error || "会话历史暂时不可用，请重试。");
        return true;
      }
      if (page > 0 && (event.revision !== this.revision
          || (event.generation ?? null) !== this.generation)) {
        this.fail("会话历史已更新，请重新读取。");
        this.cursors = [null];
        return true;
      }
      // Every page is its own bounded snapshot. Details and stale responses
      // from the previous page cannot be merged into the new one.
      const snapshot: History = { ...event, before: null };
      this.state = reduce({ ...initialState, focusedSid: this.sid,
        runtimes: { [this.sid]: createRuntime() } }, { type: "event", event: snapshot });
      this.revision = event.revision;
      this.generation = event.generation ?? null;
      this.page = page;
      this.hasMore = event.has_more && !!event.oldest_id;
      this.oldestId = event.oldest_id ?? null;
      this.cursors = this.cursors.slice(0, page + 1);
      if (this.hasMore) this.cursors.push(this.oldestId);
      this.loading = false;
      this.pendingPage = null;
      this.detailRequests.clear();
      return true;
    }
    if (event.type === "turn_detail" && event.session_id === this.sid
        && event.revision === this.revision
        && this.detailRequests.has(event.turn_id)
        && this.detailRequests.get(event.turn_id) === (event.before ?? null)) {
      this.detailRequests.delete(event.turn_id);
      this.state = reduce(this.state, { type: "event", event });
      return true;
    }
    return false;
  }
}
