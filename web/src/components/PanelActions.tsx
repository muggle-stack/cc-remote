import { useEffect, useRef, useState, type ButtonHTMLAttributes } from "react";
import { Icon } from "../icons";
import "./PanelActions.css";

type ActionProps = Omit<ButtonHTMLAttributes<HTMLButtonElement>, "children"> & { label: string };

export function PanelCloseButton({ label, className = "", ...props }: ActionProps) {
  return <button type="button" className={`iconbtn panel-action ${className}`}
    aria-label={label} title={label} {...props}><Icon name="chevrons-right" size={18} /></button>;
}

/** Keep the hit area and glyph stationary; only the arc rotates during I/O. */
export function RefreshButton({ busy, failed = false, label, disabled, className = "", ...props }:
  ActionProps & { busy: boolean; failed?: boolean }) {
  const wasBusy = useRef(false);
  const [confirmed, setConfirmed] = useState(false);
  useEffect(() => {
    if (busy || failed) setConfirmed(false);
    else if (wasBusy.current) setConfirmed(true);
    wasBusy.current = busy;
  }, [busy, failed]);
  useEffect(() => {
    if (!confirmed) return;
    const timer = window.setTimeout(() => setConfirmed(false), 900);
    return () => window.clearTimeout(timer);
  }, [confirmed]);
  return <button type="button" {...props} className={`iconbtn panel-action panel-refresh ${className}`}
    aria-label={label} aria-busy={busy} title={busy ? `${label}：更新中` : confirmed ? `${label}：已更新` : label}
    disabled={disabled || busy} data-state={busy ? "loading" : confirmed ? "done" : "ready"}>
    {busy ? <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <circle className="panel-refresh-track" cx="12" cy="12" r="8" stroke="currentColor" strokeWidth="1.6" />
      <path className="panel-refresh-arc" d="M12 4a8 8 0 0 1 8 8" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
    </svg> : confirmed ? <Icon name="check" size={18} sw={1.6} />
      : <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor"
        strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        <path d="M20 5v5h-5M20 10a8 8 0 1 0-.8 6" />
      </svg>}
  </button>;
}

export function PanelRowMenu({ label, disabled, items }: {
  label: string; disabled?: boolean;
  items: { label: string; onClick: () => void; danger?: boolean }[];
}) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (!open) return;
    root.current?.querySelector<HTMLButtonElement>('[role="menuitem"]')?.focus();
    const outside = (event: PointerEvent | FocusEvent) => {
      if (event.target instanceof Node && !root.current?.contains(event.target)) setOpen(false);
    };
    document.addEventListener("pointerdown", outside);
    document.addEventListener("focusin", outside);
    return () => {
      document.removeEventListener("pointerdown", outside);
      document.removeEventListener("focusin", outside);
    };
  }, [open]);
  return <div className="panel-row-menu" ref={root} onKeyDown={event => {
    if (event.key === "Escape" && open) {
      event.preventDefault(); event.stopPropagation(); setOpen(false); trigger.current?.focus();
    } else if (open && ["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
      event.preventDefault();
      const buttons = [...root.current!.querySelectorAll<HTMLButtonElement>('[role="menuitem"]')];
      const index = buttons.indexOf(document.activeElement as HTMLButtonElement);
      const next = event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1
        : (index + (event.key === "ArrowDown" ? 1 : -1) + buttons.length) % buttons.length;
      buttons[next]?.focus();
    }
  }}>
    <button ref={trigger} type="button" className="iconbtn panel-action" aria-label={label}
      aria-haspopup="menu" aria-expanded={open} disabled={disabled}
      onClick={() => setOpen(value => !value)}>
      <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
        <circle cx="5" cy="12" r="1.5" /><circle cx="12" cy="12" r="1.5" /><circle cx="19" cy="12" r="1.5" />
      </svg>
    </button>
    {open && <div className="panel-row-menu-items" role="menu" aria-label={label}>
      {items.map(item => <button key={item.label} type="button" role="menuitem"
        className={item.danger ? "danger" : undefined} onClick={() => {
          setOpen(false); trigger.current?.focus(); item.onClick();
        }}>{item.label}</button>)}
    </div>}
  </div>;
}
