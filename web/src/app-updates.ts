/** Device-scoped maintenance HTTP; deliberately independent of chat protocol. */
export type UpdatePhase = "submitting" | "preparing" | "downloading" | "validating" | "waiting"
  | "updating_relay" | "installing" | "verifying" | "unknown" | "succeeded" | "failed" | "blocked" | "cancelled";
export interface UpdateJob {
  id: string; version: string; previous: string; phase: UpdatePhase; reason?: string | null;
  cleanup_pending?: boolean; cancel_requested?: boolean;
  updated_at?: number;
}
export interface AppUpdateState {
  maintenance?: boolean;
  managed: boolean; enabled: boolean; current?: string; relay_version?: string;
  reason?: string | null; busy_count: number; allow_relay_update?: boolean;
  latest?: { version: string; notes: string } | null; job: UpdateJob | null;
}
export const UPDATE_TERMINAL = new Set<UpdatePhase>(["succeeded", "failed", "blocked", "cancelled"]);
const PHASES = new Set<UpdatePhase>(["submitting", "preparing", "downloading", "validating", "waiting",
  "updating_relay", "installing", "verifying", "unknown", ...UPDATE_TERMINAL]);
export const UPDATE_LABELS: Record<UpdatePhase, string> = {
  submitting: "正在启动更新", preparing: "准备更新", downloading: "下载更新包", validating: "校验更新包",
  waiting: "等待安全更新", updating_relay: "检查并更新服务器", installing: "安装设备更新",
  verifying: "验证更新结果", unknown: "更新结果待确认", succeeded: "更新完成", failed: "更新未完成",
  blocked: "更新需要处理", cancelled: "更新已取消",
};
const REASONS: Record<string, string> = {
  unsupported: "当前安装方式暂不支持应用内更新。页面更新仍可单独使用。",
  setup_required: "需要先在这台设备上启用应用内更新权限。",
  server_setup_required: "需要管理员在服务器上启用一次应用内更新权限，之后可从任意设备操作。",
  server_forbidden: "当前账号仅有设备访问权限，服务器更新需要具有全部设备权限的管理员账号。",
  control_unavailable: "设备的更新配置与本地控制连接不一致，需要检查安装配置。",
  permission_required: "设备尚未获得所需的升级权限，请先由管理员完成配置。",
  protocol_change: "这个版本需要协调服务器和全部设备的协议升级，请安排统一升级。",
  service_migration: "这个版本涉及 Claude 服务迁移，需要完成现有任务后统一升级。",
  relay_permission: "服务器也需要升级，但尚未为当前设备授权服务器升级。",
  device_unavailable: "设备未连接更新服务，或尚未安装支持应用内更新的版本。",
  outcome_pending: "尚未收到完整结果，请继续检查原更新任务。不要重复创建更新。",
  confirmation_unavailable: "当前设备尚不支持自动核实，可重试原请求；不会创建重复任务。",
  launch_unknown: "系统升级任务的启动结果尚未确认，需要检查原任务。",
  job_unknown: "原升级进程已无法确认，需要检查安装状态后再继续。",
  relay_unconfirmed: "服务器更新暂未确认，设备尚未开始安装，可以继续使用。重试会核对原服务器任务，不会重复安装。",
  rolled_back: "更新未完成，已恢复原版本，可以继续使用或重试更新。",
  worker_exited: "更新进程已退出，原版本运行正常，可以重新更新。",
  recovering: "正在核对原安装记录并恢复服务。",
  installer_running: "更新控制进程已退出，独立安装器仍在继续，正在等待安装结果。",
  recovery_required: "自动恢复尚未完成，原安装记录已保留。可以重新核对恢复结果。",
  tls_certificate: "更新连接的证书校验失败，请检查设备时间、代理证书或信任配置。",
  tls_connection: "更新服务的 TLS 连接失败，请检查网络或代理后重试。",
  ssh_auth: "服务器 SSH 身份验证失败，请检查升级账号、密钥或代理。",
  ssh_host_key: "服务器 SSH 主机身份校验失败，请核对服务器身份与已保存的主机密钥。",
  disk_space: "安装空间不足，请释放设备磁盘空间后重试。",
  network: "暂时无法连接更新服务，请稍后重新检查。",
  update_failed: "更新没有完成，请检查设备上的升级记录后再继续。",
  busy: "已有更新或其他操作正在处理，请稍后检查。",
  receipt_limit: "升级记录需要管理员维护，本次未启动新的更新。",
  release_changed: "可用版本已变化，请重新检查更新。",
  unauthorized: "登录已失效，请重新登录后检查更新。",
  forbidden: "当前登录没有这台设备的更新访问权限。",
  invalid_request: "更新请求无效，请重新打开更新窗口。",
};
export function updateReason(reason: string | null | undefined): string {
  return reason ? REASONS[reason] ?? REASONS.update_failed : "";
}

/** Short excerpts of authored release notes; never synthesize release claims. */
export function releaseHighlights(notes: string): string[] {
  const chinese = notes.split(/^##\s+中文\s*$/m)[1];
  const section = (chinese ?? notes).split(/^##\s+(English|英文)\s*$/m)[0];
  return section.split(/\r?\n/).filter(line => /^[-*]\s+/.test(line)).slice(0, 3).map(line => {
    const plain = line.replace(/^[-*]\s+/, "").replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
      .replace(/[*`]/g, "").split(/[。；]/)[0];
    return plain.length > 64 ? plain.slice(0, 64) + "…" : plain;
  });
}
export function validVersion(value: unknown): value is string {
  return typeof value === "string" && value.length < 65 && /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/.test(value);
}
export function newerVersion(target: string | undefined, current: string | undefined): boolean {
  if (!validVersion(target) || !validVersion(current)) return false;
  const a = target.split(".").map(BigInt), b = current.split(".").map(BigInt);
  for (let i = 0; i < 3; i++) { if (a[i] !== b[i]) return a[i] > b[i]; }
  return false;
}
/** A running device job keeps its own view; server work survives device switches. */
export function preferServerUpdate(device: AppUpdateState | null, server: AppUpdateState | null,
  devicePending: boolean, serverPending: boolean): boolean {
  if (devicePending || (device?.job && !UPDATE_TERMINAL.has(device.job.phase))) return false;
  if (serverPending || (server?.job && !UPDATE_TERMINAL.has(server.job.phase))) return true;
  const version = server?.latest?.version ?? device?.latest?.version;
  return newerVersion(version, server?.relay_version ?? device?.relay_version)
    && (!device?.enabled || !device.allow_relay_update);
}
export function parseUpdateJob(raw: unknown): UpdateJob | null {
  if (!raw || typeof raw !== "object") return null;
  const value = raw as Record<string, unknown>;
  if (typeof value.id !== "string" || !/^[a-f0-9]{32}$/.test(value.id)
    || !validVersion(value.version) || !validVersion(value.previous)
    || !PHASES.has(value.phase as UpdatePhase)) return null;
  return { id: value.id, version: value.version, previous: value.previous, phase: value.phase as UpdatePhase,
    reason: typeof value.reason === "string" ? value.reason : null,
    cleanup_pending: value.cleanup_pending === true, cancel_requested: value.cancel_requested === true,
    updated_at: typeof value.updated_at === "number" ? value.updated_at : undefined };
}
export function parseAppUpdate(raw: unknown): AppUpdateState {
  if (!raw || typeof raw !== "object") throw new Error("invalid_request");
  const value = raw as Record<string, unknown>;
  const latest = value.latest as Record<string, unknown> | undefined;
  const job = parseUpdateJob(value.job);
  return { managed: value.managed === true, enabled: value.enabled === true,
    maintenance: typeof value.maintenance === "boolean" ? value.maintenance
      : !!job && ["updating_relay", "installing", "verifying", "unknown"].includes(job.phase),
    current: validVersion(value.current) ? value.current : undefined,
    relay_version: validVersion(value.relay_version) ? value.relay_version : undefined,
    reason: typeof value.reason === "string" ? value.reason : null,
    busy_count: typeof value.busy_count === "number" && Number.isSafeInteger(value.busy_count)
      ? Math.max(0, value.busy_count) : 0,
    allow_relay_update: value.allow_relay_update === true,
    latest: latest && validVersion(latest.version) ? { version: latest.version,
      notes: typeof latest.notes === "string" ? latest.notes.slice(0, 4000) : "" } : null,
    job };
}

export class AppUpdateClient {
  private readonly controller = new AbortController();
  readonly machine: string;
  private readonly fetcher: typeof fetch;
  private readonly scope: "device" | "server";
  constructor(machine: string, fetcher: typeof fetch = fetch, scope: "device" | "server" = "device") {
    this.machine = machine; this.fetcher = fetcher; this.scope = scope;
  }
  close() { this.controller.abort(); }
  async call(command?: Record<string, string>): Promise<Record<string, unknown>> {
    const timeout = new AbortController();
    const timer = setTimeout(() => timeout.abort(), 35_000);
    try {
      const response = await this.fetcher.call(globalThis, this.scope === "server" ? "/api/server-updates" : `/api/updates/${encodeURIComponent(this.machine)}`, {
        method: command ? "POST" : "GET", credentials: "same-origin", cache: "no-store",
        headers: command ? { "Content-Type": "application/json" } : undefined,
        body: command ? JSON.stringify(command) : undefined,
        signal: AbortSignal.any([this.controller.signal, timeout.signal]),
      });
      if (response.status === 404) throw new Error("unsupported");
      if (response.status === 401) throw new Error("unauthorized");
      if (response.status === 403) throw new Error(this.scope === "server" ? "server_forbidden" : "forbidden");
      const body = await response.json() as Record<string, unknown>;
      if (!body || typeof body !== "object") throw new Error("invalid_request");
      if (!response.ok || body.ok === false) throw new Error(typeof body.reason === "string" ? body.reason : "network");
      return body;
    } catch (error) {
      if (this.controller.signal.aborted) throw new Error("disposed");
      if (timeout.signal.aborted) throw new Error(command?.op === "start" ? "outcome_pending" : "network");
      if (error instanceof TypeError) throw new Error(command?.op === "start" ? "outcome_pending" : "network");
      throw error;
    } finally { clearTimeout(timer); }
  }
}

export function appUpdateFeedback(state: AppUpdateState | null, now = Date.now(), subject = "设备") {
  const job = state?.job;
  if (job && (!UPDATE_TERMINAL.has(job.phase) || (job.updated_at && now / 1000 - job.updated_at < 300))) {
    return { id: `maintenance:${job.id}:${job.phase}`, label: ({ waiting: "等待更新", unknown: "待确认", succeeded: "已更新", failed: "更新失败", blocked: "需处理", cancelled: "已取消" } as Partial<Record<UpdatePhase, string>>)[job.phase] ?? "正在更新",
      tone: (job.phase === "succeeded" ? "success" : ["failed", "blocked", "unknown"].includes(job.phase)
        ? "warning" : "info") as "success" | "warning" | "info",
      message: updateReason(job.reason) || (job.phase === "waiting" ? "等待会受更新影响的操作结束，可独立运行的任务不受影响。"
        : job.phase === "succeeded" ? `${subject}已更新至 v${job.version}。`
        : `目标版本 v${job.version}，可以打开更新窗口查看进度。`) };
  }
  if (newerVersion(state?.latest?.version, state?.current)) return {
    id: `release:${state!.latest!.version}`, label: "有更新", tone: "info" as const,
    message: `cc-remote v${state!.latest!.version} 已发布，打开更新窗口查看更新范围。`,
  };
  return undefined;
}

export interface UpdateIntent { id: string; version: string; reason?: string }
export type UpdateReceipt = { status: "found"; job: UpdateJob } | { status: "missing" };
export function parseUpdateReceipt(raw: Record<string, unknown>, intent: UpdateIntent): UpdateReceipt {
  if (raw.job_id === intent.id) {
    if (raw.receipt === "missing" && raw.job === null) return { status: "missing" };
    const job = parseUpdateJob(raw.job);
    if (raw.receipt === "found" && job?.id === intent.id && job.version === intent.version) return { status: "found", job };
  }
  throw new Error("outcome_pending");
}
export class UpdateIntentStore {
  private readonly key: string;
  private readonly storage: Pick<Storage, "getItem" | "setItem" | "removeItem"> | null;
  constructor(machine: string, storage: Pick<Storage, "getItem" | "setItem" | "removeItem"> | null, scope: "device" | "server" = "device") {
    this.key = scope === "server" ? "cc_remote_server_update" : `cc_remote_ui_update:${machine}`;
    this.storage = storage;
  }
  read(): UpdateIntent | null {
    try {
      const value = JSON.parse(this.storage?.getItem(this.key) ?? "null") as UpdateIntent | null;
      return value && typeof value.id === "string" && /^[a-f0-9]{32}$/.test(value.id) && validVersion(value.version)
        ? { id: value.id, version: value.version,
          ...(typeof value.reason === "string" && value.reason.length <= 64 ? { reason: value.reason } : {}) } : null;
    } catch { return null; }
  }
  save(value: UpdateIntent) { try { this.storage?.setItem(this.key, JSON.stringify(value)); } catch { /* memory remains valid */ } }
  clear() { try { this.storage?.removeItem(this.key); } catch { /* next load reconciles against receipt */ } }
}
