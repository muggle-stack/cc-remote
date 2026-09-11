import type { GoalStatus, ServerEvent } from "./protocol";
import type { RelayWs } from "./ws";

/** Wait for native confirmation, leaving dialog drafts intact on failure. */
export class GoalApi {
  private pending = new Map<string, { sid: string; finish: (error?: Error) => void }>();
  private transport: () => RelayWs | null;
  constructor(transport: () => RelayWs | null) { this.transport = transport; }

  private request(sid: string, send: () => string | null): Promise<void> {
    const id = send();
    if (!id) return Promise.reject(new Error("连接不可用，请稍后重试。"));
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => finish(new Error("操作结果尚未确认，请重新打开目标查看。")), 30000);
      const finish = (error?: Error) => {
        if (!this.pending.delete(id)) return;
        clearTimeout(timer);
        if (error) reject(error); else resolve();
      };
      this.pending.set(id, { sid, finish });
    });
  }

  save(sid: string, objective: string | null, status: GoalStatus | null, budget: number | null) {
    return this.request(sid, () => this.transport()?.sendSetGoal(objective, status, budget, sid) ?? null);
  }

  clear(sid: string) {
    return this.request(sid, () => this.transport()?.sendClearGoal(sid) ?? null);
  }

  accept(event: ServerEvent): boolean {
    if ((event.type !== "goal_state" && event.type !== "error") || !event.request_id) return false;
    const request = this.pending.get(event.request_id);
    if (!request || request.sid !== event.sid) return false;
    request.finish(event.type === "error" ? new Error(event.message) : undefined);
    // Accepted Goal projections must still reach the runtime and other UI.
    return event.type === "error";
  }

  reset() {
    for (const request of [...this.pending.values()]) request.finish(new Error("连接已切换，请重新打开目标确认操作结果。"));
  }
}
