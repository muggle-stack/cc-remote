import type { CodexContext, ContextReport } from "../protocol";
import { workContextMetrics } from "../work-context";
export const CODEX_CONTEXT_USAGE_NOTE =
  "进度按 Codex 原生上下文估算显示；后台刷新期间保留最近有效读数。";


interface Props {
  report: ContextReport | null;
  work?: boolean;
  onAutoCompact?: () => void;
  codexContext?: CodexContext | null;
  codex?: boolean;
  dsh?: boolean;
}


export default function ContextPopover(p: Props) {
  const workMode = !!p.work;
  const work = workMode && p.report ? workContextMetrics(p.report) : null;
  const recentCodex = p.codex && p.report?.source !== "native_estimate";
  const hasCapacity = !recentCodex && (p.report?.max_tokens ?? 0) > 0;
  const usage = (tokens: number, percentage: number) => hasCapacity
    ? `${tokens.toLocaleString()} / ${p.report!.max_tokens.toLocaleString()} (${p.dsh && percentage > 0 && percentage < 1 ? "<1" : percentage.toFixed(0)}%)`
    : `${tokens.toLocaleString()} tokens`;

  return (
    <div className={"ctx-pop" + (workMode ? " work-ctx-pop" : "")}
      role="dialog" aria-label={workMode ? "Work 上下文占用" : "上下文占用"}>
      {p.report && (workMode ? work : true) ? workMode ? (
        <>
          <div className="ctx-pop-row">
            <span>{recentCodex ? "最近请求用量" : work!.hasBreakdown ? "会话新增上下文" : "上下文窗口"}</span>
            <span className="ctx-pop-nums">
              {recentCodex ? usage(p.report.total_tokens, 0)
                : usage(work!.sessionTokens, work!.sessionPercentage)}
            </span>
          </div>
          {hasCapacity && (
            <div className="ctx-pop-bar"><i style={{
              width: `${Math.min(work!.sessionPercentage, 100)}%`,
            }} /></div>
          )}
          {work!.hasBreakdown && !recentCodex && (
            <div className="work-ctx-details">
              <div className="ctx-pop-row"><span>真实总占用</span>
                <span className="ctx-pop-nums">
                  {usage(work!.totalTokens, work!.totalPercentage)}
                </span>
              </div>
              <div className="ctx-pop-row"><span>Work 启动基线</span>
                <span className="ctx-pop-nums">
                  {work!.fixedTokens.toLocaleString()}
                </span>
              </div>
            </div>
          )}
          {p.report.model && <div className="ctx-pop-foot">{p.report.model}</div>}
        </>
      ) : (
        <>
          <div className="ctx-pop-row"><span>{recentCodex ? "最近请求用量" : p.codex || (p.dsh && p.report.source === "native_estimate") ? "上下文估算" : "上下文窗口"}</span>
            <span className="ctx-pop-nums">
              {usage(p.report.total_tokens, p.report.percentage)}
            </span>
          </div>
          {hasCapacity && (
            <div className="ctx-pop-bar"><i style={{
              width: `${Math.min(p.report.percentage, 100)}%`,
            }} /></div>
          )}
          {p.report.categories.length > 0 && (
            <div className="ctx-pop-cats">
              {p.report.categories.map((category, index) => (
                <div className="ctx-pop-cat" key={index}>
                  <span className="ctx-cat-dot"
                    style={{ background: category.color }} />
                  <span className="ctx-pop-cat-name">{category.name}</span>
                  <span className="ctx-pop-cat-tok">
                    {category.tokens.toLocaleString()}
                  </span>
                </div>
              ))}
            </div>
          )}
          {p.report.model && <div className="ctx-pop-foot">{p.report.model}</div>}
        </>
      ) : <div className="ctx-pop-row"><span>上下文窗口</span><span className="ctx-pop-nums">—</span></div>}
      {p.codexContext && <>
        <div className="ctx-pop-row"><span>生效压缩阈值</span>
          <span className="ctx-pop-nums">{(p.report?.source === "native_estimate"
            ? p.report.auto_compact_threshold_tokens ?? p.codexContext.applied_threshold_tokens
            : p.codexContext.applied_threshold_tokens)?.toLocaleString()
            ?? (p.codexContext.max_context_tokens == null ? "Codex 默认值" : "待确认")}</span>
        </div>
        {p.codexContext.pending && <div className="ctx-pop-status" role="status">
          已保存上限 {p.codexContext.max_context_tokens?.toLocaleString() ?? "默认值"}，等待生效
        </div>}
      </>}
      {p.onAutoCompact && <button className="context-settings-link" onClick={p.onAutoCompact}>设置上下文上限</button>}
    </div>
  );
}
