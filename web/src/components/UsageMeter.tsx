import { useEffect, useRef, useState } from "react";
import type {
  StatusRateLimit,
  StatusRateWindow,
  StatusReport,
} from "../protocol";
import {
  activeRateLimits,
  nextRateLimitReset,
  quotaTone,
  quotaWindowsForLimits,
  quotaWindowLabel,
  remainingPercent,
} from "../rate-limit-usage";

interface Props {
  engine?: "claude" | "codex" | "dsh";
  open: boolean;
  report: StatusReport | null;
  rateLimits?: StatusRateLimit[] | null;
  error?: string | null;
  loading?: boolean;
  disabled?: boolean;
  onToggle: () => void;
  onRefresh?: () => void;
  onOpenStatus?: () => void;
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

function QuotaRow({
  label,
  window,
  showDuration = false,
}: {
  label: string;
  window: StatusRateWindow | null;
  showDuration?: boolean;
}) {
  const remaining = remainingPercent(window);
  const tone = quotaTone(remaining);
  return <div className="usage-pop-window">
    <div>
      <span>{label}</span>
      <b>{remaining == null ? "—" : `剩余 ${remaining.toFixed(0)}%`}</b>
    </div>
    <i><span className={tone} style={{ width: `${remaining ?? 0}%` }} /></i>
    <small>
      {showDuration && window?.window_duration_mins != null
        ? `${quotaWindowLabel(window.window_duration_mins)} · `
        : ""}
      {resetTime(window?.resets_at)}
    </small>
  </div>;
}

export function UsageMeter({
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
  const triggerRef = useRef<HTMLButtonElement>(null);
  const [expiryTick, setExpiryTick] = useState(0);
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
      )
    : [];
  const specializedWindows = specializedLimits.flatMap((limit) => {
    const window = limit.primary ?? limit.secondary;
    return window ? [{ limit, window }] : [];
  });
  const fiveHour = remainingPercent(quotas.fiveHour);
  const weekly = remainingPercent(quotas.weekly);
  const overall = remainingPercent(quotas.overall);
  const specializedRemaining = specializedWindows.map(
    ({ window }) => remainingPercent(window),
  );
  const minimum = [fiveHour, weekly, overall, ...specializedRemaining].filter(
    (value): value is number => value != null,
  ).reduce<number | null>(
    (current, value) => current == null ? value : Math.min(current, value),
    null,
  );
  const summaryTone = quotaTone(minimum);
  const plan = quotas.limit?.plan_type ?? visibleReport?.account?.plan_type;
  const hasOverallQuota = quotas.overall != null;
  const hasAccountQuota = !!(
    quotas.fiveHour || quotas.weekly || quotas.overall
  );
  const hasAnyQuota = hasAccountQuota || specializedWindows.length > 0;
  const provider = engine === "claude" ? "Claude" : "Codex";
  const quotaSummary = hasOverallQuota
    ? `${provider} 总额度${compactPercent(overall)}`
    : hasAccountQuota
      ? `5小时${compactPercent(fiveHour)}，每周${compactPercent(weekly)}`
      : specializedWindows.length > 0
        ? `专项周额度${compactPercent(minimum)}`
        : "额度尚未同步";
  const popoverId = `${engine}-account-usage-popover`;

  return <div className="usage-meter-wrap">
    <button
      ref={triggerRef}
      type="button"
      className={`usage-meter ${summaryTone}`}
      aria-label={`账户额度：${quotaSummary}`}
      aria-expanded={disabled ? false : open}
      aria-controls={popoverId}
      aria-haspopup="true"
      title={`${provider} 账户剩余额度`}
      disabled={disabled}
      onClick={onToggle}
    >
      {hasOverallQuota
        ? <MiniQuota label="总" value={overall} />
        : !hasAccountQuota && specializedWindows.length > 0
          ? <MiniQuota label="专项" value={minimum} />
        : <>
          <MiniQuota label="5h" value={fiveHour} />
          <MiniQuota label="周" value={weekly} />
        </>}
    </button>
    {!disabled && open && <div
      id={popoverId}
      className="ctx-pop usage-pop"
      role="region"
      aria-label={`${provider} 账户额度`}
    >
      <div className="usage-pop-head">
        <span>
          <b>账户额度</b>
          <small>
            {hasOverallQuota
              ? `当前账户的 ${provider} 总额度`
              : engine === "claude"
                ? "5 小时、每周与模型专项窗口"
                : "5 小时与每周窗口"}
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
            ? "尚未收到 Claude Code 的额度事件；原生额度更新后会自动同步。"
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
      ) : hasAccountQuota ? <>
          <QuotaRow label="5 小时额度" window={quotas.fiveHour} />
          <QuotaRow label="每周额度" window={quotas.weekly} />
        </> : null}
        {specializedWindows.map(({ limit, window }) => (
          <QuotaRow
            key={limit.limit_id ?? limit.limit_name ?? "specialized"}
            label={`${limit.limit_name ?? "模型"} 专项周额度`}
            window={window}
          />
        ))}
      </>}
      <div className="usage-pop-actions">
        <span>{error ? "旧账户数据已隐藏"
          : loading ? "正在更新…"
          : engine === "claude"
            ? "Claude Code 原生额度事件自动同步"
            : "来自当前 Codex 账户"}</span>
        {onRefresh && <button type="button" onClick={onRefresh}
          disabled={loading}>刷新</button>}
        {onOpenStatus && <button type="button" onClick={onOpenStatus}>完整状态</button>}
      </div>
    </div>}
  </div>;
}
