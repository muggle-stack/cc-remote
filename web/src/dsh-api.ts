import type { DshCommandResult, DshDownloadChunk, DshGoalAction, DshReadKind, DshReadResult, ServerEvent } from "./protocol";
import type { RelayWs } from "./ws";

type Reply = DshReadResult | DshCommandResult | DshDownloadChunk;
export type DshRead = (sid: string, kind: DshReadKind, options?: {
  query?: string; job_id?: string; target_sid?: string; before_seq?: number; signal?: AbortSignal;
}) => Promise<DshReadResult>;

/** Request identity and session identity both bind every private response. */
export type DshApiClient = Pick<DshApi, "read" | "act" | "goal" | "download" | "control" | "watchTerminal">;
export class DshApi {
  private pending = new Map<string, { sid: string; finish: (value?: Reply, error?: Error) => void }>();
  private terminals = new Map<string, { sid: string; receive: (value: DshReadResult) => void; fail: () => void }>();
  watchTerminal(sid: string, lease: string, receive: (value: DshReadResult) => void, fail: () => void) {
    this.terminals.set(lease, { sid, receive, fail });
    return () => { this.terminals.delete(lease); };
  }
  private transport: () => RelayWs | null;
  constructor(transport: () => RelayWs | null) { this.transport = transport; }

  private request<T extends Reply>(sid: string, send: () => string | null, signal?: AbortSignal, timeout = 30000): Promise<T> {
    if (signal?.aborted) return Promise.reject(new DOMException("Aborted", "AbortError"));
    const id = send();
    if (!id) return Promise.reject(new Error("连接不可用，请重试"));
    return new Promise((resolve, reject) => {
      const abort = () => finish(undefined, new DOMException("Aborted", "AbortError"));
      const timer = setTimeout(() => finish(undefined, new Error("等待响应超时；请刷新确认当前状态")), timeout);
      const finish = (value?: Reply, error?: Error) => {
        if (!this.pending.delete(id)) return;
        clearTimeout(timer); signal?.removeEventListener("abort", abort);
        if (error) reject(error); else resolve(value as T);
      };
      this.pending.set(id, { sid, finish });
      signal?.addEventListener("abort", abort, { once: true });
      if (signal?.aborted) abort();
    });
  }

  read: DshRead = (sid, kind, options = {}) => {
    const { signal, ...fields } = options;
    return this.request(sid, () => this.transport()?.sendReadDsh(sid, kind, fields) ?? null, signal);
  };

  act(sid: string, target: string, action: "queue" | "steer" | "stop", prompt = "") {
    return this.request<DshCommandResult>(sid,
      () => this.transport()?.sendActDshSubagent(sid, target, action, prompt) ?? null);
  }

  control(sid: string, kind: "job_stop" | "plugin_enable" | "plugin_disable" | "manage" | "terminal", value: string) {
    let timeout = 30000;
    if (kind === "manage") {
      try {
        if (["bundle_install", "bundle_update", "market_install"].includes(JSON.parse(value)?.action)) timeout = 660000;
      } catch { /* The native request validator reports malformed controls. */ }
    }
    return this.request<DshCommandResult>(sid, () => this.transport()?.sendDshControl(sid, kind, value) ?? null, undefined,
      timeout);
  }

  async goal(sid: string, action: DshGoalAction) {
    const result = await this.request<DshCommandResult>(sid,
      () => this.transport()?.sendActDshGoal(sid, action) ?? null);
    if (result.status !== "success") throw new Error(result.text || "操作结果未确认，请刷新目标后重试。");
  }

  async download(sid: string, progress: (value: number) => void, signal: AbortSignal): Promise<Blob> {
    const transport = this.transport();
    const { download } = await import("./dsh-download");
    return download((...args) => this.request<DshDownloadChunk>(...args), () => transport, sid, progress, signal);
  }

  accept(event: ServerEvent): boolean {
    if (!["dsh_read_result", "dsh_command_result", "dsh_download_chunk", "error"].includes(event.type)
        || !("request_id" in event) || !event.request_id) return false;
    const terminal = this.terminals.get(event.request_id);
    if (terminal && event.sid === terminal.sid && event.type === "dsh_read_result" && event.kind === "terminal_output") {
      terminal.receive(event); return true;
    }
    const pending = this.pending.get(event.request_id);
    if (!pending || event.sid !== pending.sid) return false;
    if (event.type === "error") pending.finish(undefined, new Error(event.message));
    else pending.finish(event as Reply);
    return true;
  }

  reset() {
    for (const terminal of [...this.terminals.values()]) terminal.fail();
    this.terminals.clear();
    for (const pending of [...this.pending.values()]) pending.finish(undefined, new Error("连接或会话已切换；操作结果请刷新确认"));
  }
}
