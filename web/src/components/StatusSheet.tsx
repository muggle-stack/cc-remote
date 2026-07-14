import type { ReactNode } from "react";
import type { StatusRateLimit, StatusRateWindow, StatusReport } from "../protocol";
import { Icon } from "../icons";
import { accountStatsNote } from "../status-capabilities";

interface Props {
  open: boolean;
  report: StatusReport | null;
  onClose: () => void;
  onRefresh: () => void;
}

const show = (value: unknown, empty = "—") => value == null || value === "" ? empty : String(value);
const yesNo = (value: boolean | null | undefined) => value == null ? "—" : value ? "是" : "否";

function tokenCount(value?: number | null): string {
  if (value == null) return "—";
  return value.toLocaleString();
}

function resetTime(value?: number | null): string {
  if (!value) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit",
  }).format(new Date(value * 1000));
}

function Row({ label, children, mono = false }: { label: string; children: ReactNode; mono?: boolean }) {
  return <div className="status-row"><span>{label}</span><b className={mono ? "mono" : ""}>{children}</b></div>;
}

function RateWindow({ name, window }: { name: string; window?: StatusRateWindow | null }) {
  if (!window) return null;
  const pct = window.used_percent == null ? null : Math.max(0, Math.min(100, window.used_percent));
  return <div className="status-rate-window">
    <div><span>{name}</span><b>{pct == null ? "—" : `${pct}% 已用`}</b></div>
    <i><span style={{ width: `${pct ?? 0}%` }} /></i>
    <small>{window.window_duration_mins ? `${window.window_duration_mins} 分钟窗口` : "滚动窗口"} · {resetTime(window.resets_at)} 重置</small>
  </div>;
}

function RateLimit({ limit }: { limit: StatusRateLimit }) {
  return <div className="status-rate-card">
    <div className="status-rate-head"><b>{limit.limit_name || limit.limit_id || "Codex"}</b><span>{limit.plan_type || "—"}</span></div>
    {limit.rate_limit_reached_type && <div className="status-rate-warning">{limit.rate_limit_reached_type}</div>}
    <RateWindow name="主要限额" window={limit.primary} />
    <RateWindow name="次要限额" window={limit.secondary} />
  </div>;
}

export function StatusSheet({ open, report, onClose, onRefresh }: Props) {
  if (!open) return null;
  const thread = report?.thread;
  const runtime = report?.runtime;
  const context = report?.context;
  const usage = report?.usage;
  const statsNote = accountStatsNote(report?.account);
  return <>
    <div className="scrim show" onClick={onClose} />
    <section className="sheet show status-sheet" role="dialog" aria-modal="true" aria-label="Codex Status">
      <div className="sheet-grip" />
      <header className="status-sheet-head">
        <span className="status-sheet-icon"><Icon name="cpu" size={19} /></span>
        <span><b>Codex Status</b><small>来自当前 app-server 的实时状态</small></span>
        <button className="status-refresh" onClick={onRefresh}>刷新</button>
        <button onClick={onClose} aria-label="关闭"><Icon name="close" size={17} /></button>
      </header>
      {!report ? <div className="status-loading"><span />正在读取 thread、config 和 account…</div> :
      <div className="status-sheet-scroll">
        <section className="status-section">
          <h3>线程</h3>
          <div className="status-grid">
            <Row label="状态"><span className={`status-thread-state status-thread-${thread?.status}`}>{show(thread?.status)}</span></Row>
            <Row label="活动标记">{thread?.active_flags?.length ? thread.active_flags.join(" · ") : "无"}</Row>
            <Row label="工作目录" mono>{show(thread?.cwd)}</Row>
            <Row label="Thread ID" mono>{show(thread?.thread_id)}</Row>
            <Row label="Session ID" mono>{show(thread?.session_id)}</Row>
            <Row label="来源">{show(thread?.source)}</Row>
            <Row label="CLI 版本" mono>{show(thread?.cli_version)}</Row>
            <Row label="临时线程">{yesNo(thread?.ephemeral)}</Row>
          </div>
        </section>

        <section className="status-section">
          <h3>运行配置</h3>
          <div className="status-grid">
            <Row label="App server" mono>{show(runtime?.app_server_version)}</Row>
            <Row label="模型" mono>{show(runtime?.model)}</Row>
            <Row label="Provider">{show(runtime?.model_provider)}</Row>
            <Row label="思考强度" mono>{show(runtime?.reasoning_effort)}</Row>
            <Row label="Service tier">{show(runtime?.service_tier, "标准")}</Row>
            <Row label="审批策略">{show(runtime?.approval_policy)}</Row>
            <Row label="Sandbox">{show(runtime?.sandbox_mode)}</Row>
            <Row label="Web search">{show(runtime?.web_search)}</Row>
          </div>
        </section>

        <section className="status-section">
          <h3>上下文</h3>
          <div className="status-context-card">
            <div><strong>{context?.percentage == null ? "—" : `${context.percentage.toFixed(1)}%`}</strong><span>{tokenCount(context?.used_tokens)} / {tokenCount(context?.max_tokens)} tokens</span></div>
            <i><span style={{ width: `${Math.max(0, Math.min(100, context?.percentage ?? 0))}%` }} /></i>
          </div>
        </section>

        {report.account && <section className="status-section">
          <h3>账户</h3>
          <div className="status-grid">
            <Row label="认证方式">{report.account.auth_type}</Row>
            <Row label="订阅计划">{show(report.account.plan_type)}</Row>
            <Row label="需要 OpenAI 登录">{yesNo(report.account.requires_openai_auth)}</Row>
          </div>
          {statsNote && report.rate_limits.length === 0 && !usage &&
            <div className="status-capability-note">{statsNote}</div>}
        </section>}

        {report.rate_limits.length > 0 && <section className="status-section">
          <h3>使用限额</h3>
          <div className="status-rates">{report.rate_limits.map((limit, index) => <RateLimit key={limit.limit_id || index} limit={limit} />)}</div>
        </section>}

        {usage && <section className="status-section">
          <h3>账户用量</h3>
          <div className="status-usage">
            <div><small>累计 Token</small><b>{tokenCount(usage.lifetime_tokens)}</b></div>
            <div><small>当前连续天数</small><b>{show(usage.current_streak_days)}</b></div>
            <div><small>单日峰值</small><b>{tokenCount(usage.peak_daily_tokens)}</b></div>
            <div><small>最长 Turn</small><b>{usage.longest_running_turn_sec == null ? "—" : `${usage.longest_running_turn_sec}s`}</b></div>
          </div>
        </section>}

        {report.component_errors.length > 0 && <section className="status-partial">
          <b>部分状态暂不可用</b>
          {report.component_errors.map((error, index) => <span key={index}>{error}</span>)}
        </section>}
      </div>}
    </section>
  </>;
}
