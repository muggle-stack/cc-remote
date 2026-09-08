import type { Engine, ServerEvent, TurnFileChangesPage } from "./protocol";

export type LoadTurnFilePage = (
  turnId: string, revision: string, offset: number, signal: AbortSignal,
) => Promise<TurnFileChangesPage>;

interface PageScope {
  sid: string; engine: Engine; turnId: string; revision: string; offset: number;
}

/** Private reads, bound to the requested immutable version, never to focus. */
export class TurnFilePageRequests {
  private pending = new Map<string, {
    scope: PageScope;
    finish: (page?: TurnFileChangesPage, error?: string) => void;
  }>();
  private retired = new Map<string, string>();

  private timeoutMs: number;
  constructor(timeoutMs = 15_000) { this.timeoutMs = timeoutMs; }

  request(scope: PageScope, send: () => string | null, signal: AbortSignal): Promise<TurnFileChangesPage> {
    if (signal.aborted || this.pending.size >= 32) return Promise.reject(new Error("请求已取消，请重试。"));
    const id = send();
    if (!id) return Promise.reject(new Error("连接暂不可用，请稍后重试。"));
    return new Promise((resolve, reject) => {
      const finish = (page?: TurnFileChangesPage, error = "文件清单加载失败，请重试。") => {
        if (!this.pending.delete(id)) return;
        clearTimeout(timer);
        signal.removeEventListener("abort", abort);
        this.retired.set(id, scope.sid);
        while (this.retired.size > 64) this.retired.delete(this.retired.keys().next().value!);
        if (page) resolve(page);
        else reject(new Error(error));
      };
      const abort = () => finish(undefined, "请求已取消。");
      const timer = setTimeout(() => finish(undefined, "加载超时，请重试。"), this.timeoutMs);
      this.pending.set(id, { scope, finish });
      signal.addEventListener("abort", abort, { once: true });
      if (signal.aborted) abort();
    });
  }

  accept(event: ServerEvent): boolean {
    if (event.type !== "turn_file_changes_page" && event.type !== "error") return false;
    const id = event.request_id;
    const pending = id ? this.pending.get(id) : undefined;
    if (!pending) return event.type === "turn_file_changes_page"
      || !!(id && this.retired.get(id) === event.sid);
    const { scope, finish } = pending;
    if (event.sid !== scope.sid) return event.type === "turn_file_changes_page";
    if (event.type === "error") {
      finish(undefined, event.message);
    } else {
      if (event.engine !== scope.engine || event.turn_id !== scope.turnId
          || event.revision !== scope.revision || event.offset !== scope.offset) return true;
      const end = event.offset + event.files.length;
      if (event.files.length === 0 || event.files.length > 64 || end > event.total_files
          || new Set(event.files.map((row) => row.path)).size !== event.files.length
          || (event.next_offset ?? null) !== (end < event.total_files ? end : null)) {
        finish(undefined, "文件清单不完整，请刷新本轮后重试。");
      } else finish(event);
    }
    return true;
  }

  clear(): void {
    for (const { finish } of this.pending.values()) finish(undefined, "连接已更新，请重新加载。");
  }
}
