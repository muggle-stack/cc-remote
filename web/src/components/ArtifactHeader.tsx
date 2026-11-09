import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { Icon } from "../icons";
import "./ArtifactHeader.css";

/** Keep preview navigation separate from mode controls; media needs only one row. */
export function ArtifactHeader({ title, path, onBack, backLabel, onClose, onReload,
  reloadDisabled, tabs, modes, actions, convertedFrom }: {
  title: string; path: string;
  onBack?: () => void; backLabel?: string;
  onClose: () => void; onReload?: () => void; reloadDisabled?: boolean;
  tabs?: ReactNode; modes?: ReactNode; actions: ReactNode; convertedFrom?: string;
}) {
  const [open, setOpen] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  const id = useId();

  useEffect(() => {
    if (!open) return;
    const node = menu.current!;
    node.focus({ preventScroll: true });
    const outside = (event: PointerEvent | FocusEvent) => {
      if (event.target instanceof Node && !node.contains(event.target)
          && !trigger.current?.contains(event.target)) setOpen(false);
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || event.defaultPrevented || event.isComposing) return;
      event.preventDefault(); event.stopPropagation();
      setOpen(false); trigger.current?.focus({ preventScroll: true });
    };
    // Sandboxed previews cannot bubble their pointer events to this document.
    const blur = () => setOpen(false);
    document.addEventListener("pointerdown", outside, true);
    document.addEventListener("focusin", outside);
    document.addEventListener("keydown", escape, true);
    window.addEventListener("blur", blur);
    return () => {
      document.removeEventListener("pointerdown", outside, true);
      document.removeEventListener("focusin", outside);
      document.removeEventListener("keydown", escape, true);
      window.removeEventListener("blur", blur);
    };
  }, [open]);

  const controls = <div className="artifact-preview-actions">{actions}</div>;
  return <header className="artifact-preview-head">
    {tabs && <div className="artifact-preview-tabs">{tabs}</div>}
    <div className="artifact-preview-navigation">
      {onBack && <button type="button" className="iconbtn" onClick={onBack}
        aria-label={backLabel || "返回"} title={backLabel || "返回"}>
        <Icon name="chevron-left" size={18} />
      </button>}
      <button type="button" className="artifact-file-trigger" ref={trigger}
        aria-label={`文件信息：${title}`} aria-haspopup="dialog"
        aria-expanded={open} aria-controls={open ? id : undefined}
        onClick={() => setOpen(value => !value)}>
        <span className="artifact-title">{title}</span>
        {convertedFrom && <span className="artifact-converted" title="转换后的 PDF 预览">
          {convertedFrom.toUpperCase()} → PDF
        </span>}
        <Icon name="chev" size={14} />
      </button>
      {!modes && controls}
      <button type="button" className="iconbtn" onClick={onClose} aria-label="收起" title="收起">
        <Icon name="chevrons-right" />
      </button>
      {open && <div className="artifact-file-menu" role="dialog" aria-label="文件信息"
        tabIndex={-1} id={id} ref={menu}>
        <small>文件路径</small><p>{path || title}</p>
        {convertedFrom && <small>预览格式：{convertedFrom.toUpperCase()} → PDF</small>}
        {onReload && <button type="button" disabled={reloadDisabled} onClick={() => {
          setOpen(false); trigger.current?.focus({ preventScroll: true }); onReload();
        }}><Icon name="refresh" size={16} />重新读取</button>}
      </div>}
    </div>
    {modes && <div className="artifact-preview-toolbar">{modes}{controls}</div>}
  </header>;
}
