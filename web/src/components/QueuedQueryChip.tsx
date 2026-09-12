import { Icon } from "../icons";
import type { PendingQuery } from "../reducer";

interface ChipProps {
  query: PendingQuery;
  onOpen: (query: PendingQuery) => void;
  onRemove: () => void;
}

export function QueuedQueryChip({
  query, onOpen, onRemove,
}: ChipProps) {
  const fallback = (query.imageCount ?? query.images?.length ?? 0) > 0
    ? "图片" : "附件";
  const badge = query.queueState === "failed"
    ? "未发送"
    : query.queueState === "submitting"
      ? "提交中"
      : query.queueKind === "replace"
        ? "替换"
        : "排队";
  return (
    <span className={`qchip${query.queueError ? " error" : ""}`}>
      <button type="button" className="qsummary"
        disabled={!query.msg_id}
        aria-label="查看排队消息"
        onClick={() => onOpen(query)}>
        <span className="qbadge">{badge}</span>
        <span className="qt">{query.prompt || fallback}</span>
      </button>
      <button type="button" className="qx" onClick={onRemove}
        aria-label="移出队列">
        <Icon name="close" size={12} />
      </button>
    </span>
  );
}
