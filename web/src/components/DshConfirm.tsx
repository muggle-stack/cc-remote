import { useEffect, useRef, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { Icon } from "../icons";

/** A native modal isolates confirmation from the task list and nested panels. */
export function DshConfirm({ title, busy, onClose, children }: {
  title: string; busy: boolean; onClose: () => void; children: ReactNode;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const close = useRef(onClose); close.current = onClose;
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const dialog = ref.current!;
    dialog.showModal();
    return () => { dialog.close(); if (previous?.isConnected) previous.focus({ preventScroll: true }); };
  }, []);
  useEffect(() => {
    const dialog = ref.current;
    if (!busy && dialog && document.activeElement === dialog) {
      dialog.querySelector<HTMLButtonElement>("footer button:not(:disabled)")?.focus({ preventScroll: true });
    }
  }, [busy]);
  return createPortal(<dialog ref={ref} className="dsh-confirm" aria-label={title}
    onCancel={event => { event.preventDefault(); if (!busy) close.current(); }}
    onClick={event => { if (event.target === event.currentTarget && !busy) {
      const rect = event.currentTarget.getBoundingClientRect();
      if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) close.current();
    } }}>
    <header><h2>{title}</h2><button className="iconbtn" disabled={busy} aria-label="关闭确认框" onClick={onClose}><Icon name="close" size={18} /></button></header>
    {children}
  </dialog>, document.body);
}
