import type { AgentDetailRun } from "../agent-detail";
import { finalTextBlocks, presentableProcessBlocks } from "../process-blocks";
import { ClaudeWorking, Icon } from "../icons";
import { MessageBlock } from "./MessageBlock";
import { ProcessTimeline } from "./ProcessTimeline";
import { PanelResizer } from "./PanelResizer";

function statusLabel(run: AgentDetailRun): string {
  if (run.status === "running" || run.status === "pending") return "运行中";
  if (run.status === "succeeded") return "已完成";
  if (run.status === "failed") return "失败";
  if (["cancelled", "interrupted", "declined"].includes(run.status)) return "已停止";
  return "状态未知";
}

export function AgentDetailPanel({ run, canGoBack, onBack, onClose, onRetry,
  onLoadEarlier, onOpenAgent, onOpenFile }: {
  run: AgentDetailRun;
  canGoBack: boolean;
  onBack: () => void;
  onClose: () => void;
  onRetry: () => void;
  onLoadEarlier: () => void;
  onOpenAgent: (runId: string, title?: string) => void;
  onOpenFile?: (path: string, line?: number) => void;
}) {
  const process = presentableProcessBlocks(run.blocks, "claude");
  const final = finalTextBlocks(run.blocks);
  const done = !["running", "pending"].includes(run.status);
  return (
    <aside className="agent-detail-panel" aria-label="协作代理详情"
      data-lock-horizontal-swipe="true">
      <PanelResizer ariaLabel="调整协作代理面板宽度" />
      <header className="agent-detail-header">
        <button type="button" className="icon-btn" onClick={onBack}
          disabled={!canGoBack} aria-label="返回上一级协作代理">
          <Icon name="back" size={17} />
        </button>
        <div className="agent-detail-heading">
          <strong>{run.title}</strong>
          <span className={`agent-detail-status agent-status-${run.status}`}>
            {statusLabel(run)}
          </span>
        </div>
        <button type="button" className="icon-btn" onClick={onClose}
          aria-label="关闭协作代理详情"><Icon name="close" size={17} /></button>
      </header>
      <div className="agent-detail-scroll">
        {run.loading && run.events.length === 0 && (
          <div className="agent-detail-empty" role="status">
            <span className="spinner" aria-hidden="true" />
            <span>正在读取协作代理过程…</span>
          </div>
        )}
        {run.error && (
          <div className="agent-detail-error" role="alert">
            <span>{run.error}</span>
            <button type="button" onClick={onRetry}>重试</button>
          </div>
        )}
        {run.hasMore && run.oldestCursor && (
          <button type="button" className="agent-detail-more"
            disabled={run.loading} onClick={onLoadEarlier}>
            {run.loading ? "正在加载…" : "加载更早过程"}
          </button>
        )}
        {process.length > 0 && (
          <ProcessTimeline blocks={run.blocks} done={done}
            active={!done} engine="claude" openOverride
            onOpenAgent={onOpenAgent} onOpenFile={onOpenFile} />
        )}
        {final.map((block) => (
          <MessageBlock key={block.message_id} text={block.text}
            done={block.done} onOpenFile={onOpenFile} />
        ))}
        {!done && <div className="turn-working agent-detail-working" role="status">
          <ClaudeWorking size={24} />
          <span className="turn-working-tx">子代理处理中</span>
        </div>}
        {!run.loading && !run.error && run.blocks.length === 0 && (
          <div className="agent-detail-empty">这个协作代理暂时没有可展示的过程。</div>
        )}
      </div>
    </aside>
  );
}
