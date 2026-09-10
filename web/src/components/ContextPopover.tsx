import type { CodexContext, ContextReport } from "../protocol";
import type { WorkContextMetrics } from "../work-context";


interface Props {
  report: ContextReport | null;
  loading?: boolean;
  deferred?: boolean;
  error?: string | null;
  work?: WorkContextMetrics | null;
  onAutoCompact?: () => void;
  codexContext?: CodexContext | null;
}


export default function ContextPopover(p: Props) {
  const workMode = p.work !== undefined;
  const hasCapacity = (p.report?.max_tokens ?? 0) > 0;
  const status = p.loading
    ? "正在读取真实上下文…"
    : p.deferred
      ? "会话正在工作，结束后自动更新。"
      : p.error || "";
  const usage = (tokens: number, percentage: number) => hasCapacity
    ? `${tokens.toLocaleString()} / ${p.report!.max_tokens.toLocaleString()} (${percentage.toFixed(0)}%)`
    : `${tokens.toLocaleString()} tokens`;
  const statusNode = status && (
    <div className={"ctx-pop-status" + (p.error ? " error" : "")}
      role={p.error ? "alert" : undefined}>{status}</div>
  );
  const loadingNode = (
    <div className="ctx-pop-loading" role={p.error ? "alert" : undefined}>
      {status || "正在读取真实上下文…"}
    </div>
  );

  return (
    <div className={"ctx-pop" + (workMode ? " work-ctx-pop" : "")}
      role="dialog" aria-label={workMode ? "Work 上下文占用" : "上下文占用"}>
      {p.report && (workMode ? p.work : true) ? workMode ? (
        <>
          <div className="ctx-pop-row">
            <span>{p.work!.hasBreakdown ? "会话新增上下文" : "上下文窗口"}</span>
            <span className="ctx-pop-nums">
              {usage(p.work!.sessionTokens, p.work!.sessionPercentage)}
            </span>
          </div>
          {hasCapacity && (
            <div className="ctx-pop-bar"><i style={{
              width: `${Math.min(p.work!.sessionPercentage, 100)}%`,
            }} /></div>
          )}
          {p.work!.hasBreakdown && (
            <div className="work-ctx-details">
              <div className="ctx-pop-row"><span>真实总占用</span>
                <span className="ctx-pop-nums">
                  {usage(p.work!.totalTokens, p.work!.totalPercentage)}
                </span>
              </div>
              <div className="ctx-pop-row"><span>Work 启动基线</span>
                <span className="ctx-pop-nums">
                  {p.work!.fixedTokens.toLocaleString()}
                </span>
              </div>
            </div>
          )}
          {p.report.model && <div className="ctx-pop-foot">{p.report.model}</div>}
          {statusNode}
        </>
      ) : (
        <>
          <div className="ctx-pop-row"><span>上下文窗口</span>
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
          {statusNode}
        </>
      ) : loadingNode}
      {p.codexContext && <>
        <div className="ctx-pop-row"><span>自动压缩阈值</span>
          <span className="ctx-pop-nums">{p.codexContext.applied_threshold_tokens?.toLocaleString() ?? "Codex 默认值"}</span>
        </div>
        {p.codexContext.pending && <div className="ctx-pop-status" role="status">
          已保存 {p.codexContext.threshold_tokens?.toLocaleString() ?? "默认值"}，等待生效
        </div>}
      </>}
      {p.onAutoCompact && <button className="context-settings-link" onClick={p.onAutoCompact}>设置自动压缩阈值</button>}
    </div>
  );
}
