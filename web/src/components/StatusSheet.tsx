import { useEffect, useRef, type ReactNode } from "react";
import type {
  Notice, RateLimitResetResult, StatusRateLimit,
  StatusRateLimitResetCredit, StatusRateWindow, StatusReport,
} from "../protocol";
import { Icon } from "../icons";
import {
  resetCreditConfirmation, resetCreditOutcomeMessage, resetCreditPresentation,
} from "../rate-limit-reset";
import { accountStatsNote } from "../status-capabilities";
import { statusNotices } from "../notice-presentation";
import {
  codexAccountPlan,
  quotaTone,
  quotaWindowLabel,
  remainingPercent,
} from "../rate-limit-usage";

interface Props {
  open: boolean;
  report: StatusReport | null;
  notices?: Notice[];
  error?: string | null;
  resetCreditLoading?: boolean;
  resetCreditResult?: RateLimitResetResult | null;
  resetCreditDisabled?: boolean;
  onClose: () => void;
  onRefresh: () => void;
  onConsumeResetCredit?: (creditId?: string | null) => boolean;
  onDismissNotice?: (noticeId: string) => void;
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

function confirmResetCredit(credit?: StatusRateLimitResetCredit): boolean {
  return window.confirm(resetCreditConfirmation(credit));
}

function ResetCredit({ credit, loading, disabled, onConsume }: {
  credit: StatusRateLimitResetCredit;
  loading: boolean;
  disabled: boolean;
  onConsume?: (credit: StatusRateLimitResetCredit) => void;
}) {
  const available = credit.status === "available";
  const copy = resetCreditPresentation(credit);
  return <article className="status-reset-credit">
    <span>
      <b>{copy.title}</b>
      {copy.description && <small>{copy.description}</small>}
      <small>{credit.expires_at
        ? `${resetTime(credit.expires_at)} 到期`
        : "长期有效"}</small>
    </span>
    {available && onConsume
      ? <button type="button" disabled={loading || disabled}
          onClick={() => onConsume(credit)}>
          {loading ? "使用中…" : "使用"}
        </button>
      : <em>{credit.status === "redeeming" ? "处理中"
        : credit.status === "redeemed" ? "已使用" : "不可用"}</em>}
  </article>;
}

function Row({ label, children, mono = false }: { label: string; children: ReactNode; mono?: boolean }) {
  return <div className="status-row"><span>{label}</span><b className={mono ? "mono" : ""}>{children}</b></div>;
}

function RateWindow({ name, window, monthly = false }: { name: string; window?: StatusRateWindow | null; monthly?: boolean }) {
  if (!window) return null;
  const remaining = remainingPercent(window);
  const tone = quotaTone(remaining);
  return <div className="status-rate-window">
    <div><span>{name}</span><b>{remaining == null ? "—" : `剩余 ${remaining.toFixed(0)}%`}</b></div>
    <i><span className={tone} style={{ width: `${remaining ?? 0}%` }} /></i>
    <small>{monthly ? "每月额度" : quotaWindowLabel(window.window_duration_mins)} · {resetTime(window.resets_at)} 重置</small>
  </div>;
}

function RateLimit({ limit }: { limit: StatusRateLimit }) {
  return <div className="status-rate-card">
    <div className="status-rate-head"><b>{limit.limit_name || limit.limit_id || "Codex"}</b><span>{limit.plan_type || "—"}</span></div>
    {limit.rate_limit_reached_type && <div className="status-rate-attention">当前限额已用尽，请等待重置。</div>}
    <RateWindow name="主要限额" window={limit.primary} />
    <RateWindow name="次要限额" window={limit.secondary} />
    <RateWindow name="每月额度" window={limit.monthly} monthly />
  </div>;
}

export function StatusSheet({ open, report, notices = [], error, onClose,
  resetCreditLoading = false, resetCreditResult,
  resetCreditDisabled = false, onRefresh, onConsumeResetCredit,
  onDismissNotice }: Props) {
  const resetLaunchRef = useRef(false);
  useEffect(() => {
    if (!resetCreditLoading) resetLaunchRef.current = false;
  }, [resetCreditLoading]);
  if (!open) return null;
  const thread = report?.thread;
  const runtime = report?.runtime;
  const context = report?.context;
  const usage = report?.usage;
  const resetCredits = report?.reset_credits;
  const resetCreditRows = resetCredits?.credits ?? [];
  const detailedAvailable = resetCreditRows.filter(
    (credit) => credit.status === "available").length;
  const hasUndetailedCredits = !!resetCredits
    && resetCredits.available_count > detailedAvailable;
  const consumeResetCredit = (credit?: StatusRateLimitResetCredit) => {
    if (!onConsumeResetCredit || resetLaunchRef.current
        || !confirmResetCredit(credit)) return;
    resetLaunchRef.current = true;
    if (!onConsumeResetCredit(credit?.id ?? null)) {
      resetLaunchRef.current = false;
    }
  };
  const statsNote = accountStatsNote(report?.account);
  const presentedNotices = statusNotices(notices);
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
      {error && <div className="status-partial" role="alert"><b>状态读取失败</b><span>{error}</span></div>}
      {!report && !error ? <div className="status-loading"><span />正在读取 thread、config 和 account…</div> : report ?
      <div className="status-sheet-scroll">
        {presentedNotices.length > 0 && <section className="status-section">
          <h3>需要关注</h3>
          <div className="status-attention-list">{presentedNotices.map((notice) =>
            <article key={notice.notice_id}>
              <span><b>{notice.title}</b><small>{notice.message}</small></span>
              {onDismissNotice && <button type="button"
                onClick={() => onDismissNotice(notice.notice_id)}
                aria-label={`关闭提示：${notice.title}`}>
                <Icon name="close" size={14} />
              </button>}
            </article>)}</div>
        </section>}
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
            <Row label="执行环境" mono>{show(runtime?.permission_profile)}</Row>
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
            <Row label="订阅计划">{show(error ? null : codexAccountPlan(report))}</Row>
            <Row label="需要 OpenAI 登录">{yesNo(report.account.requires_openai_auth)}</Row>
          </div>
          {statsNote && report.rate_limits.length === 0 && !usage &&
            <div className="status-capability-note">{statsNote}</div>}
        </section>}

        {report.rate_limits.length > 0 && <section className="status-section">
          <h3>使用限额</h3>
          <div className="status-rates">{report.rate_limits.map((limit, index) => <RateLimit key={limit.limit_id || index} limit={limit} />)}</div>
        </section>}

        {(resetCredits || resetCreditResult) &&
          <section className="status-section">
            <h3>重置券</h3>
            {resetCredits && <div className="status-reset-summary">
              <span><b>{resetCredits.available_count}</b> 张可用</span>
              <small>仅重置符合条件的 Codex 额度窗口</small>
            </div>}
            {resetCreditRows.length > 0 && <div className="status-reset-list">
              {resetCreditRows.map((credit) => <ResetCredit
                key={credit.id}
                credit={credit}
                loading={resetCreditLoading}
                disabled={resetCreditDisabled
                  || (resetCredits?.available_count ?? 0) < 1}
                onConsume={consumeResetCredit}
              />)}
            </div>}
            {hasUndetailedCredits && onConsumeResetCredit &&
              <button type="button" className="status-reset-next"
                disabled={resetCreditLoading || resetCreditDisabled}
                onClick={() => consumeResetCredit()}>
                {resetCreditLoading ? "使用中…" : "使用下一张"}
              </button>}
            {resetCreditResult && <div className="status-reset-result" role="status">
              {resetCreditOutcomeMessage(resetCreditResult.outcome)}
            </div>}
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
          <b>部分信息暂不可用</b>
          <span>稍后刷新即可，不影响当前会话。</span>
        </section>}
      </div> : null}
    </section>
  </>;
}
