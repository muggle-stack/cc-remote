import type { Ref } from "react";
import type { Engine } from "../protocol";
import { EngineIcon, Icon } from "../icons";

export function QuestionCollapseButton({ onCollapse, onPointerDown }: {
  onCollapse: () => void;
  onPointerDown: () => void;
}) {
  return <button type="button" className="qa-collapse" aria-label="收起问题"
    title="收起问题，查看聊天" onPointerDown={onPointerDown} onClick={onCollapse}>
    <Icon name="chev" size={18} />
  </button>;
}

/** Local presentation only. The owning native question remains unanswered. */
export function QuestionResume({ engine, onResume, buttonRef, page, total, pending, sourceLabel }: {
  engine: Engine;
  onResume: () => void;
  buttonRef: Ref<HTMLButtonElement>;
  page?: number;
  total?: number;
  pending?: boolean;
  sourceLabel?: string;
}) {
  return <button type="button" className="question-resume" ref={buttonRef}
    onClick={onResume} aria-haspopup="dialog" data-lock-horizontal-swipe>
    <EngineIcon engine={engine} size={21} />
    <span className="qa-resume-label">{pending ? "回答提交中…" : "等待你回答"}
      {sourceLabel && <small className="qa-resume-source">{sourceLabel}</small>}
    </span>
    {total !== undefined && total > 1 && <span className="qa-resume-progress">{page} / {total}</span>}
    <span className="qa-resume-action">{pending ? "查看回答" : "继续回答"}<Icon name="chev" size={16} /></span>
  </button>;
}
