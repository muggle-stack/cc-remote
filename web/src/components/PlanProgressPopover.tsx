import ReactMarkdown from "react-markdown";
import {
  useCallback,
  useContext,
  useEffect,
  useId,
  useRef,
  useState,
  type CSSProperties,
  type RefObject,
} from "react";
import { createPortal } from "react-dom";
import { PanelVisible } from "../right-panel-presentation";
import { useAnchoredPopoverGeometry } from "../chat-dialog-geometry";
import type { ProcessBlock } from "../domain/conversation";
import { Icon } from "../icons";
import { claudePlanDismissLabel, planProgressPresentation } from "../plan-progress";
import {
  cancelDraggedPointer,
  PointerTapGuard,
  releaseDraggedPointer,
} from "../pointer-tap";

export function PlanProgressContent({ block, detailLoading = false, onDismiss, disabled }: {
  onDismiss?: () => void;
  disabled?: boolean;
  block: ProcessBlock;
  detailLoading?: boolean;
}) {
  const presentation = planProgressPresentation(block, detailLoading);
  const steps = block.plan ?? [];
  const dismissLabel = onDismiss ? claudePlanDismissLabel(block.claudePlan) : null;
  const completionHintId = useId();
  return <div className={`plan-progress-content${presentation.stale ? " stale" : ""}`}>
    <header>
      <span className={`plan-progress-mark${presentation.complete ? " complete" : ""}${presentation.failed ? " failed" : ""}${presentation.stale ? " stale" : ""}`}>
        <Icon name={presentation.complete ? "verify" : "plan"} size={15} />
      </span>
      <span>
        <b>计划</b>
        <small>{presentation.stateLabel}</small>
      </span>
      {presentation.progressLabel !== presentation.stateLabel
        && <strong>{presentation.progressLabel}</strong>}
      {dismissLabel && <button type="button" className="plan-completion-action"
        disabled={disabled} onClick={onDismiss} aria-describedby={completionHintId}>
        {dismissLabel}
      </button>}
    </header>
    {presentation.description && <div className="plan-document">
      <ReactMarkdown skipHtml>{presentation.description}</ReactMarkdown>
    </div>}
    {steps.length > 0 ? <ol>
      {steps.map((entry, index) => (
        <li key={`${index}-${entry.step}`} className={`plan-step-${entry.status}`}>
          <span aria-hidden="true">{entry.status === "completed"
            ? <Icon name="verify" size={14} />
            : entry.status === "inProgress" ? <i /> : <em>{index + 1}</em>}</span>
          <span>{entry.step}</span>
        </li>
      ))}
    </ol> : presentation.fallbackDetail
      ? <pre className="plan-progress-fallback">{presentation.fallbackDetail}</pre>
      : block.claudePlan ? null : <div className="plan-progress-empty">
          {detailLoading ? "正在加载计划…"
            : block.done ? "未提供计划步骤。" : "等待计划步骤…"}
        </div>}
    {dismissLabel && <footer className="plan-completion">
      <small id={completionHintId}>收起后仍可在历史记录中查看计划。</small>
    </footer>}
  </div>;
}

export function PlanProgressFloatingCard({ anchorRef, block, open,
  onOpenChange, id, detailLoading = false, compact = false, onDismiss, disabled }: {
  onDismiss?: () => void;
  disabled?: boolean;
  anchorRef: RefObject<HTMLElement | null>;
  block: ProcessBlock;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  id: string;
  detailLoading?: boolean;
  compact?: boolean;
}) {
  const cardRef = useRef<HTMLElement>(null);
  const visible = useContext(PanelVisible);
  open = open && visible;
  const position = useAnchoredPopoverGeometry({
    open,
    anchorRef,
    maxWidth: block.claudePlan ? 520 : compact ? 360 : 400,
    maxHeight: block.claudePlan ? 560 : compact ? 440 : 500,
  });

  useEffect(() => {
    if (!open) return;
    const closeOutside = (event: PointerEvent) => {
      const target = event.target;
      if (!(target instanceof Node)) return;
      if (anchorRef.current?.contains(target)
        || cardRef.current?.contains(target)) return;
      onOpenChange(false);
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") onOpenChange(false);
    };
    document.addEventListener("pointerdown", closeOutside, true);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("pointerdown", closeOutside, true);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [anchorRef, onOpenChange, open]);

  if (!open || !position) return null;
  const { placement, ...style } = position;
  return createPortal(
    <section ref={cardRef} id={id}
      className={`plan-progress-popover place-${placement}${compact ? " compact" : ""}`}
      data-placement={placement}
      role="dialog" aria-modal="false" aria-label="计划进度"
      style={style}>
      <PlanProgressContent block={block} detailLoading={detailLoading} onDismiss={onDismiss} disabled={disabled} />
    </section>,
    document.body,
  );
}

export function PlanProgressPopover({ block, openOverride, onOpenChange,
  detailLoading = false, onNeedDetail }: {
  block: ProcessBlock;
  openOverride?: boolean;
  onOpenChange?: (open: boolean) => void;
  detailLoading?: boolean;
  onNeedDetail?: () => void;
}) {
  const visible = useContext(PanelVisible);
  const [uncontrolledOpen, setUncontrolledOpen] = useState(false);
  const open = openOverride ?? uncontrolledOpen;
  const rootRef = useRef<HTMLDivElement>(null);
  const tapGuard = useRef(new PointerTapGuard());
  const labelId = useId();
  const presentation = planProgressPresentation(block, detailLoading);

  const setOpen = useCallback((next: boolean) => {
    setUncontrolledOpen(next);
    onOpenChange?.(next);
  }, [onOpenChange]);
  useEffect(() => { if (!visible) setOpen(false); }, [visible, setOpen]);

  return <div ref={rootRef} className="plan-progress-control">
    <button type="button"
      className={`plan-progress-trigger${presentation.complete ? " complete" : ""}${presentation.failed ? " failed" : ""}${presentation.stale ? " stale" : ""}`}
      aria-expanded={open} aria-controls={labelId}
      aria-label={`查看计划进度，${presentation.progressLabel}`}
      title={`计划进度 · ${presentation.progressLabel}`}
      onPointerDown={(event) => {
        tapGuard.current.pointerDown(
          event.pointerId, event.clientX, event.clientY);
        event.currentTarget.setPointerCapture?.(event.pointerId);
      }}
      onPointerMove={(event) => {
        if (tapGuard.current.pointerMove(
          event.pointerId, event.clientX, event.clientY,
        )) {
          releaseDraggedPointer(
            event.currentTarget, event.pointerId, event.pointerType);
        }
      }}
      onPointerUp={(event) => tapGuard.current.pointerUp(event.pointerId)}
      onPointerCancel={(event) => {
        cancelDraggedPointer(
          tapGuard.current,
          event.currentTarget, event.pointerId, event.pointerType);
      }}
      onClick={(event) => {
        if (!tapGuard.current.consumeClick(event.detail)) {
          event.preventDefault();
          return;
        }
        const next = !open;
        // Cached projection steps paint immediately after refresh, but they do
        // not replace the authoritative turn detail. Opening the popover must
        // still refresh when its owner advertises deferred detail.
        if (next) onNeedDetail?.();
        setOpen(next);
      }}>
      <span className="plan-progress-ring" aria-hidden="true"
        style={{ "--plan-progress": `${presentation.progress * 3.6}deg` } as CSSProperties}>
        <Icon name={presentation.complete ? "verify" : "plan"} size={12} />
      </span>
    </button>
    <PlanProgressFloatingCard anchorRef={rootRef} block={block} open={open}
      onOpenChange={setOpen} id={labelId} detailLoading={detailLoading} />
  </div>;
}
