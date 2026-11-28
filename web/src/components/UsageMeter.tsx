import type { Engine } from "../protocol";
import { useEffect, useRef, useState, type ReactNode } from "react";
import type {
  AntigravityQuotaBucket,
  StatusRateLimit,
  StatusRateWindow,
  StatusReport,
} from "../protocol";
import type { AntigravityQuotaView } from "../antigravity-quota";
import {
  activeRateLimits,
  codexAccountPlan,
  nextRateLimitReset,
  quotaTone,
  quotaWindowsForLimits,
  quotaWindowLabel,
  remainingPercent,
  type QuotaTone,
} from "../rate-limit-usage";

interface Props {
  engine?: Engine;
  open: boolean;
  report: StatusReport | null;
  rateLimits?: StatusRateLimit[] | null;
  error?: string | null;
  loading?: boolean;
  disabled?: boolean;
  onToggle: () => void;
  onRefresh?: () => void;
  onOpenStatus?: () => void;
  antigravityQuota?: AntigravityQuotaView;
  model?: string | null;
}

const EMPTY_RATE_LIMITS: StatusRateLimit[] = [];

function compactPercent(value: number | null): string {
  return value == null ? "—" : `${value.toFixed(0)}%`;
}

function resetTime(value?: number | null): string {
  if (!value) return "重置时间未知";
  return `${new Intl.DateTimeFormat("zh-CN", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value * 1000))} 重置`;
}

function MiniQuota({
  label,
  value,
}: {
  label: string;
  value: number | null;
}) {
  const tone = quotaTone(value);
  return <span className="usage-meter-line">
    <b>{label}</b>
    <i><em className={tone} style={{ width: `${value ?? 0}%` }} /></i>
  </span>;
}

const UNSYNCED_QUOTA = <>
  <MiniQuota label="5h" value={null} />
  <MiniQuota label="周" value={null} />
</>;

function QuotaRow({
  label,
  window,
  showDuration = false,
  hideUnknownReset = false,
}: {
  label: string;
  window: StatusRateWindow | null;
  showDuration?: boolean;
  hideUnknownReset?: boolean;
}) {
  const remaining = remainingPercent(window);
  const tone = quotaTone(remaining);
  return <div className="usage-pop-window">
    <div>
      <span>{label}</span>
      <b>{remaining == null ? "—" : `剩余 ${remaining.toFixed(0)}%`}</b>
    </div>
    <i><span className={tone} style={{ width: `${remaining ?? 0}%` }} /></i>
    {(!hideUnknownReset || window?.resets_at) && <small>
      {showDuration && window?.window_duration_mins != null
        ? `${quotaWindowLabel(window.window_duration_mins)} · `
        : ""}
      {resetTime(window?.resets_at)}
    </small>}
  </div>;
}

function MeterFrame({ engine, open, disabled, onToggle, provider, summary, tone, trigger, children }:
  Pick<Props, "engine" | "open" | "disabled" | "onToggle"> & {
    provider: string;
    summary: string;
    tone: QuotaTone;
    trigger: ReactNode;
    children: ReactNode;
  }) {
  const triggerRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (!open) return;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      onToggle();
      window.requestAnimationFrame(() => triggerRef.current?.focus());
    };
    document.addEventListener("keydown", closeOnEscape);
    return () => document.removeEventListener("keydown", closeOnEscape);
  }, [onToggle, open]);
  const popoverId = `${engine}-account-usage-popover`;
  return <div className="usage-meter-wrap">
    <button ref={triggerRef} type="button" className={`usage-meter ${tone}`}
      aria-label={`账户额度：${summary}`} aria-expanded={disabled ? false : open}
      aria-controls={popoverId} aria-haspopup="true" title={`${provider} 账户剩余额度`}
      disabled={disabled} onClick={onToggle}>
      {trigger}
    </button>
    {!disabled && open && <div id={popoverId} className="ctx-pop usage-pop" role="region"
      aria-label={`${provider} 账户额度`}>
      {children}
    </div>}
  </div>;
}

export function UsageMeter(props: Props) {
  return props.engine === "antigravity"
    ? <GeminiUsageMeter {...props} /> : <ExistingUsageMeter {...props} />;
}

const GEMINI_WINDOWS: Record<string, { short: string; label: string; order: number }> = {
  "5h": { short: "5h", label: "5 小时额度", order: 0 },
  weekly: { short: "周", label: "每周额度", order: 1 },
  monthly: { short: "月", label: "每月额度", order: 2 },
};

function quotaModelKey(label?: string | null): string {
  return (label ?? "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/-+$/, "")
    .replace(/-(?:low|medium|high|xhigh|extra(?:-high)?|max|thinking)$/, "");
}

function geminiQuotaWindow(bucket: AntigravityQuotaBucket): StatusRateWindow {
  return { used_percent: (1 - bucket.remaining_fraction) * 100, resets_at: bucket.resets_at };
}

function GeminiUsageMeter({ open, disabled, onToggle, onRefresh, antigravityQuota, model }: Props) {
  useEffect(() => {
    if (!open || disabled || !onRefresh) return;
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") onRefresh();
    }, 60_000);
    return () => window.clearInterval(timer);
  }, [open, disabled, onRefresh]);
  const reading = antigravityQuota?.reading;
  const nativePlan = reading?.plan;
  const plan = nativePlan === "Antigravity Starter Quota" ? "Starter" : nativePlan;
  const now = Date.now() / 1000;
  const groups = (reading?.groups ?? []).map(group => ({
    ...group,
    buckets: group.buckets.filter(bucket => !bucket.resets_at || bucket.resets_at > now)
      .sort((a, b) => (GEMINI_WINDOWS[a.window]?.order ?? 3) - (GEMINI_WINDOWS[b.window]?.order ?? 3)),
  })).filter(group => group.buckets.length);
  const key = quotaModelKey(model);
  const matches = key ? groups.filter(group => group.models.some(label => quotaModelKey(label) === key)) : [];
  const current = matches.length === 1 ? matches[0].buckets : [];
  const minimum = current.length ? Math.min(...current.map(bucket => bucket.remaining_fraction * 100)) : null;
  const summary = current.length
    ? current.map(bucket => `${GEMINI_WINDOWS[bucket.window]?.label ?? bucket.name}剩余${compactPercent(bucket.remaining_fraction * 100)}`).join("，")
    : "Gemini 额度尚未同步";
  return <MeterFrame engine="antigravity" provider="Gemini" open={open} disabled={disabled}
    onToggle={onToggle} summary={summary} tone={quotaTone(minimum)}
    trigger={current.length ? current.slice(0, 3).map(bucket => <MiniQuota key={bucket.id}
      label={GEMINI_WINDOWS[bucket.window]?.short ?? "限"} value={bucket.remaining_fraction * 100} />)
      : UNSYNCED_QUOTA}>
    <div className="usage-pop-head">
      <span><b>Gemini 账户额度</b></span>
      {plan && <em className="usage-pop-plan" title={nativePlan ?? undefined}>{plan}</em>}
    </div>
    {groups.map(group => <section className="usage-pop-group"
      key={`${group.name}:${group.buckets[0].id}`}>
      <h4>{group.name === "Gemini Models" ? "Gemini 模型"
        : group.name === "Claude and GPT models" ? "Claude / GPT 模型" : group.name}</h4>
      {group.buckets.map(bucket => <QuotaRow key={bucket.id} hideUnknownReset
        label={GEMINI_WINDOWS[bucket.window]?.label ?? bucket.name}
        window={geminiQuotaWindow(bucket)} />)}
    </section>)}
    {!groups.length && <div className="ctx-pop-loading" role="status">
      {antigravityQuota?.loading ? "正在读取账户额度…"
        : reading?.error === "refresh_failed" ? "额度读取失败，稍后自动更新。"
          : "当前账户暂无可显示的额度。"}
    </div>}
    {groups.length > 0 && reading?.error === "refresh_failed" && <p className="usage-query-note" role="status">
      {reading.observed_at ? `额度读取失败，显示 ${new Date(reading.observed_at * 1000).toLocaleString()} 的数据。`
        : "额度读取失败，显示上次同步的数据。"}
    </p>}
  </MeterFrame>;
}

function ExistingUsageMeter({
  engine = "codex",
  open,
  report,
  rateLimits,
  error,
  loading = false,
  disabled = false,
  onToggle,
  onRefresh,
  onOpenStatus,
}: Props) {
  const [expiryTick, setExpiryTick] = useState(0);

  const sourceLimits = rateLimits ?? report?.rate_limits ?? EMPTY_RATE_LIMITS;
  useEffect(() => {
    const reset = nextRateLimitReset(sourceLimits);
    if (reset == null) return;
    const delay = Math.max(0, reset * 1000 - Date.now() + 50);
    const timer = window.setTimeout(
      () => setExpiryTick((current) => current + 1),
      Math.min(delay, 2_147_483_647),
    );
    return () => window.clearTimeout(timer);
  }, [expiryTick, sourceLimits]);

  // A failed refresh after an account-switch barrier means the retained report
  // may belong to the previous account. Quarantine it until a fresh snapshot
  // succeeds instead of presenting stale values as current.
  const visibleReport = error ? null : report;
  const visibleLimits = activeRateLimits(
    error ? EMPTY_RATE_LIMITS : sourceLimits,
  );
  const quotas = quotaWindowsForLimits(
    visibleLimits, engine === "claude" ? "claude" : "codex",
  );
  const specializedLimits = engine === "claude"
    ? visibleLimits.filter((limit) =>
        limit.limit_id === "claude-seven-day-opus"
        || limit.limit_id === "claude-seven-day-sonnet"
        || limit.limit_id === "claude-seven-day-fable"
      )
    : [];
  const specializedWindows = specializedLimits.flatMap((limit) => {
    const window = limit.primary ?? limit.secondary;
    return window ? [{ limit, window }] : [];
  });
  const fiveHour = remainingPercent(quotas.fiveHour);
  const weekly = remainingPercent(quotas.weekly);
  const monthly = remainingPercent(quotas.monthly);
  const overall = remainingPercent(quotas.overall);
  const specializedRemaining = specializedWindows.map(
    ({ window }) => remainingPercent(window),
  );
  const minimum = [fiveHour, weekly, monthly, overall, ...specializedRemaining].filter(
    (value): value is number => value != null,
  ).reduce<number | null>(
    (current, value) => current == null ? value : Math.min(current, value),
    null,
  );
  const summaryTone = quotaTone(minimum);
  const plan = error ? null : engine === "claude"
    ? sourceLimits.find((limit) => limit.limit_id === "claude")?.plan_type
    : codexAccountPlan(visibleReport, sourceLimits);
  const hasOverallQuota = quotas.overall != null;
  const hasAccountQuota = !!(
    quotas.fiveHour || quotas.weekly || quotas.monthly || quotas.overall
  );
  const periodRows = [
    { key: "5h", label: "5 小时额度", window: quotas.fiveHour },
    { key: "周", label: "每周额度", window: quotas.weekly },
    { key: "月", label: "每月额度", window: quotas.monthly },
  ].filter((row) => row.window != null);
  const hasAnyQuota = hasAccountQuota || specializedWindows.length > 0;
  const claudeReadError = engine === "claude"
    ? visibleReport?.component_errors.find((reason) => reason.startsWith("rate_limits:"))
    : undefined;
  const queryState = engine === "claude" ? visibleReport?.quota_query_state : null;
  const queryNote = queryState === "disabled"
    ? "主动查询已关闭，保留原生额度同步。"
    : queryState === "unconfigured"
      ? "未配置额度查询来源，请先在本机配置。"
      : null;
  const claudeReadNote = claudeReadError?.endsWith("usage helper unavailable")
    ? "未配置主动额度查询，等待原生同步。"
    : claudeReadError ? "额度刷新失败，请重试。" : null;
  const provider = engine === "claude" ? "Claude" : "Codex";
  const quotaSummary = hasOverallQuota
    ? `${provider} 总额度${compactPercent(overall)}`
    : hasAccountQuota
      ? periodRows.map(({ label, window }) =>
          `${label}剩余${compactPercent(remainingPercent(window))}`).join("，")
      : specializedWindows.length > 0
        ? `专项周额度${compactPercent(minimum)}`
        : "额度尚未同步";
  return <MeterFrame engine={engine} provider={provider} open={open} disabled={disabled}
    onToggle={onToggle} summary={quotaSummary} tone={summaryTone}
    trigger={hasOverallQuota
        ? <MiniQuota label="总" value={overall} />
        : !hasAccountQuota && specializedWindows.length > 0
          ? <MiniQuota label="专项" value={minimum} />
        : hasAccountQuota ? periodRows.map(({ key, window }) => (
          <MiniQuota key={key} label={key} value={remainingPercent(window)} />
        )) : UNSYNCED_QUOTA}>
      <div className="usage-pop-head">
        <span>
          <b>账户额度</b>
          <small>
            {hasOverallQuota
              ? `当前账户的 ${provider} 总额度`
              : "当前账号剩余额度"}
          </small>
        </span>
        {plan && <em>{plan}</em>}
      </div>
      {error ? (
        <div className="ctx-pop-loading" role="alert">{error}</div>
      ) : !visibleReport && loading ? (
        <div className="ctx-pop-loading">正在读取账户额度…</div>
      ) : !hasAnyQuota ? (
        <div className="ctx-pop-loading">
          {engine === "claude"
            ? claudeReadNote ?? "额度尚未同步，请刷新。"
            : visibleReport?.account?.auth_type === "chatgpt"
            ? "账户已登录；本次额度读取失败，请刷新重试。"
            : "当前 Codex app-server 暂未提供账户额度。"}
        </div>
      ) : <>{hasOverallQuota ? (
        <QuotaRow
          label={`${provider} 总额度`}
          window={quotas.overall}
          showDuration
        />
      ) : periodRows.map(({ key, label, window }) => (
          <QuotaRow key={key} label={label} window={window} />
        ))}
        {specializedWindows.map(({ limit, window }) => (
          <QuotaRow
            key={limit.limit_id ?? limit.limit_name ?? "specialized"}
            label={`${limit.limit_name ?? "模型"} 专项周额度`}
            window={window}
          />
        ))}
      </>}
      {hasAnyQuota && claudeReadNote && <div className="ctx-pop-loading" role="status">
        {claudeReadNote} 当前显示上次同步数据。
      </div>}
      {queryNote && <p className="usage-query-note" role="status">{queryNote}</p>}
      <div className="usage-pop-actions">
        <span>{error ? "旧账户数据已隐藏"
          : loading ? "正在更新…"
          : engine === "claude" && queryState === "enabled"
            ? "主动查询已开启" : `来自当前 ${provider} 账户`}</span>
        {onRefresh && <button type="button" onClick={onRefresh}
          disabled={loading}>刷新</button>}
        {onOpenStatus && <button type="button" onClick={onOpenStatus}>完整状态</button>}
      </div>
  </MeterFrame>;
}
