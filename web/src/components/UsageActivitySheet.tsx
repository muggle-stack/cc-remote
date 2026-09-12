import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
} from "react";
import type { StatusReport } from "../protocol";
import { Icon } from "../icons";
import { CenteredSheet } from "./CenteredSheet";
import { accountStatsNote } from "../status-capabilities";
import {
  buildUsageActivityCalendar,
  compactTokenCount,
  formatUsageDate,
  formatUsageDuration,
  USAGE_ACTIVITY_WEEKS,
} from "../usage-activity";

interface Props {
  open: boolean;
  report: StatusReport | null;
  error?: string | null;
  loading?: boolean;
  hasSession: boolean;
  onClose: () => void;
  onRefresh: () => void;
}

const EMPTY_BUCKETS = [] as const;

function tokenCountLabel(value?: number | null): string {
  const compact = compactTokenCount(value);
  return compact === "—" ? compact : `${compact} 个 Token`;
}

function streak(value?: number | null): string {
  return value == null ? "—" : `${value} 天`;
}

export function UsageActivitySheet({
  open,
  report,
  error,
  loading = false,
  hasSession,
  onClose,
  onRefresh,
}: Props) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const usage = error ? null : report?.usage;
  const usageComponentError = report?.component_errors.find(
    (item) => item.startsWith("usage:"),
  );
  const buckets = usage?.daily_usage_buckets ?? EMPTY_BUCKETS;
  const calendar = useMemo(
    () => buildUsageActivityCalendar(buckets),
    [buckets],
  );
  const [selectedDate, setSelectedDate] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    const latest = [...calendar.days].reverse().find(
      (day) => !day.future && day.tokens > 0,
    ) ?? [...calendar.days].reverse().find((day) => !day.future);
    setSelectedDate((current) => current && calendar.days.some(
      (day) => day.date === current && !day.future,
    ) ? current : latest?.date ?? null);
    const frame = window.requestAnimationFrame(() => {
      const viewport = scrollRef.current;
      if (viewport) viewport.scrollLeft = viewport.scrollWidth;
    });
    return () => window.cancelAnimationFrame(frame);
  }, [calendar, open]);

  if (!open) return null;
  const selected = calendar.days.find((day) => day.date === selectedDate);
  const statsNote = accountStatsNote(report?.account);
  const style = {
    "--usage-activity-weeks": USAGE_ACTIVITY_WEEKS,
  } as CSSProperties;

  return <CenteredSheet open={open} label="Codex 使用活动" onClose={onClose}
    className="usage-activity-sheet" maxWidth={900} maxHeight={780} header={false}>
      <header className="usage-activity-head">
        <span className="usage-activity-icon"><Icon name="calendar" size={19} /></span>
        <span>
          <b>使用活动</b>
          <small>当前 Codex 账户 · 最近 53 周</small>
        </span>
        {report?.account?.plan_type && (
          <em>{report.account.plan_type}</em>
        )}
        <button type="button" className="usage-activity-refresh"
          onClick={onRefresh} disabled={loading || !hasSession}>
          <Icon name="refresh" size={15} />
          <span>{loading ? "更新中" : "刷新"}</span>
        </button>
        <button type="button" className="usage-activity-close"
          onClick={onClose} aria-label="关闭使用活动">
          <Icon name="close" size={17} />
        </button>
      </header>

      <div className="usage-activity-scroll">
        {error ? (
          <div className="usage-activity-state" role="alert">
            <Icon name="calendar" size={22} />
            <b>活动读取失败</b>
            <span>{error}</span>
          </div>
        ) : !report && loading ? (
          <div className="usage-activity-state">
            <span className="usage-activity-spinner" />
            <b>正在读取账户活动…</b>
          </div>
        ) : !report && !hasSession ? (
          <div className="usage-activity-state">
            <Icon name="calendar" size={22} />
            <b>尚未连接 Codex 会话</b>
            <span>打开任意一条 Codex 对话后即可读取账号活动。</span>
          </div>
        ) : !usage ? (
          <div className="usage-activity-state">
            <Icon name="calendar" size={22} />
            <b>当前账号没有活动统计</b>
            <span>{statsNote ?? usageComponentError
              ?? "当前 Codex app-server 暂未返回 Token 活动。"}</span>
          </div>
        ) : <>
          <section className="usage-activity-stats" aria-label="账户活动摘要">
            <div title={tokenCountLabel(usage.lifetime_tokens)}>
              <b>{compactTokenCount(usage.lifetime_tokens)}</b>
              <span>累计 Token</span>
            </div>
            <div title={tokenCountLabel(usage.peak_daily_tokens)}>
              <b>{compactTokenCount(usage.peak_daily_tokens)}</b>
              <span>单日峰值</span>
            </div>
            <div>
              <b>{formatUsageDuration(usage.longest_running_turn_sec)}</b>
              <span>最长任务</span>
            </div>
            <div>
              <b>{streak(usage.current_streak_days)}</b>
              <span>当前连续</span>
            </div>
            <div>
              <b>{streak(usage.longest_streak_days)}</b>
              <span>最长连续</span>
            </div>
          </section>

          <section className="usage-activity-panel">
            <header>
              <span><b>Token 活动</b><small>颜色越深，相对用量越高</small></span>
              <em>每日</em>
            </header>
            {buckets.length > 0 ? <>
              <div ref={scrollRef} className="usage-activity-viewport">
                <div className="usage-activity-calendar" style={style}>
                  <div className="usage-activity-grid" role="grid"
                    aria-label="最近 53 周每日 Token 活动">
                    {calendar.days.map((day) => (
                      <button key={day.date} type="button"
                        className="usage-activity-tile"
                        data-level={day.level}
                        data-future={day.future ? "true" : undefined}
                        data-date={day.date}
                        disabled={day.future}
                        tabIndex={day.tokens > 0 ? 0 : -1}
                        aria-label={`${formatUsageDate(day.date)}，使用了 ${tokenCountLabel(day.tokens)}`}
                        aria-pressed={selectedDate === day.date}
                        title={`${formatUsageDate(day.date)} 使用了 ${tokenCountLabel(day.tokens)}`}
                        onClick={() => setSelectedDate(day.date)}
                        onFocus={() => setSelectedDate(day.date)}
                      />
                    ))}
                  </div>
                  <div className="usage-activity-months" aria-hidden="true">
                    {calendar.months.map((month) => (
                      <span key={`${month.week}-${month.label}`}
                        style={{ gridColumnStart: month.week + 1 }}>
                        {month.label}
                      </span>
                    ))}
                  </div>
                </div>
              </div>
              <div className="usage-activity-caption">
                <span aria-live="polite">{selected
                  ? `${formatUsageDate(selected.date)} · ${selected.tokens > 0
                    ? tokenCountLabel(selected.tokens)
                    : "无活动"}`
                  : "选择方格查看当天用量"}</span>
                <span className="usage-activity-legend" aria-label="颜色强度：少到多">
                  <small>少</small>
                  {[0, 1, 2, 3, 4].map((level) => (
                    <i key={level} data-level={level} />
                  ))}
                  <small>多</small>
                </span>
              </div>
            </> : (
              <div className="usage-activity-empty">
                当前账号尚未返回每日 Token 记录。
              </div>
            )}
          </section>
        </>}
      </div>
  </CenteredSheet>;
}
