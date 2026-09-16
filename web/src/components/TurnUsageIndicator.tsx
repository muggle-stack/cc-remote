import { useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { TokenUsage } from "../protocol";
import { useAnchoredPopoverGeometry } from "../chat-dialog-geometry";
import { compactTokens } from "../turn-usage";
import "./TurnUsageIndicator.css";

export function TurnUsageIndicator({ usage }: { usage: TokenUsage | undefined }) {
  const [open, setOpen] = useState(false);
  const pinned = useRef(false);
  const trigger = useRef<HTMLButtonElement>(null);
  const card = useRef<HTMLDivElement>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const id = useId();
  const position = useAnchoredPopoverGeometry({ open, anchorRef: trigger,
    maxWidth: 300, maxHeight: 260, minimumHeight: 190, align: "start" });
  const cancelClose = () => {
    if (timer.current) clearTimeout(timer.current);
    timer.current = null;
  };
  const closeSoon = () => {
    cancelClose();
    timer.current = setTimeout(() => { if (!pinned.current) setOpen(false); }, 160);
  };
  useEffect(() => () => { if (timer.current) clearTimeout(timer.current); }, []);
  useEffect(() => {
    if (!open) return;
    const close = () => { pinned.current = false; setOpen(false); };
    const outside = (event: PointerEvent) => {
      if (event.target instanceof Node && !trigger.current?.contains(event.target)
        && !card.current?.contains(event.target)) close();
    };
    const escape = (event: KeyboardEvent) => { if (event.key === "Escape") close(); };
    document.addEventListener("pointerdown", outside, true);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("pointerdown", outside, true);
      document.removeEventListener("keydown", escape);
    };
  }, [open]);
  if (!usage) return null;
  const rows = [
    ["输入", usage.input_tokens], ["输出", usage.output_tokens],
    ["缓存读取", usage.cache_read_tokens], ["缓存写入", usage.cache_write_tokens],
  ] as const;
  const style = position ? {
    left: position.left, top: position.top, width: position.width,
    maxHeight: position.maxHeight,
  } : undefined;
  return <>
    <button ref={trigger} type="button" className="turn-usage-trigger"
      aria-label="查看 token 用量" aria-haspopup="dialog" aria-expanded={open}
      aria-controls={open ? id : undefined} aria-live="off"
      onPointerEnter={(event) => {
        if (event.pointerType === "mouse") { cancelClose(); setOpen(true); }
      }}
      onPointerLeave={(event) => { if (event.pointerType === "mouse") closeSoon(); }}
      onFocus={() => { cancelClose(); setOpen(true); }}
      onBlur={closeSoon}
      onClick={() => {
        cancelClose(); pinned.current = !pinned.current; setOpen(pinned.current);
      }}>
      <span>↑ {compactTokens(usage.input_tokens)}</span>
      <span>↓ {compactTokens(usage.output_tokens)} <span className="turn-usage-unit">tokens</span></span>
    </button>
    {open && position && createPortal(
      <div ref={card} id={id} className={`turn-usage-popover place-${position.placement}`}
        data-placement={position.placement} style={style} role="dialog" aria-label="Token 用量"
        onPointerEnter={cancelClose} onPointerLeave={closeSoon}>
        <div className="turn-usage-heading">本轮用量 <span>tokens</span></div>
        {rows.map(([label, value]) => <div className="turn-usage-row" key={label}>
          <span>{label}</span><strong>{value == null ? "—" : value.toLocaleString("en-US")}</strong>
        </div>)}
      </div>, document.body)}
  </>;
}
