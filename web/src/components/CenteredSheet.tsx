import { useEffect, useRef, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { useChatDialogGeometry } from "../chat-dialog-geometry";
import { Icon } from "../icons";
import "./CenteredSheet.css";

/** Shared selection surface, centered in the visible chat on every device. */
export function CenteredSheet({ open, label, onClose, children, className = "",
  maxWidth = 520, maxHeight = 720, header = true }: {
  open: boolean;
  label: string;
  onClose: () => void;
  children: ReactNode;
  className?: string;
  maxWidth?: number;
  maxHeight?: number;
  header?: boolean;
}) {
  const ref = useRef<HTMLElement>(null);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  const geometry = useChatDialogGeometry({
    open, maxWidth, maxHeight, minimumHeight: 360,
  });
  const visible = open && !!geometry;
  useEffect(() => {
    if (!visible) return;
    const previous = document.activeElement as HTMLElement | null;
    // Do not focus a text field and summon the phone keyboard on opening.
    ref.current?.focus({ preventScroll: true });
    const onKey = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.isComposing || event.keyCode === 229) return;
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        closeRef.current();
      } else if (event.key === "Tab") {
        const nodes = [...(ref.current?.querySelectorAll<HTMLElement>(
          'button:not(:disabled), input:not(:disabled), textarea:not(:disabled), a[href], [tabindex="0"]',
        ) ?? [])].filter(node => node.getClientRects().length > 0);
        const current = nodes.indexOf(document.activeElement as HTMLElement);
        if (current < 0 || current === (event.shiftKey ? 0 : nodes.length - 1)) {
          event.preventDefault();
          (event.shiftKey ? nodes.at(-1) : nodes[0])?.focus();
        }
      }
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      if (previous?.isConnected) previous.focus({ preventScroll: true });
    };
  // Viewport and keyboard geometry changes must not reset focus or scrolling.
  }, [visible]);
  if (!open || (!geometry && typeof document !== "undefined")) return null;
  const content = <>
    <div className="scrim show centered-sheet-scrim" onClick={onClose} />
    <section ref={ref} tabIndex={-1} className={`sheet show centered-sheet ${className}`}
      role="dialog" aria-modal="true" aria-label={label} style={geometry ?? undefined}>
      {header && <header className="centered-sheet-head">
        <span className="sheet-title">{label}</span>
        <button type="button" onClick={onClose} aria-label={`关闭${label}`}>
          <Icon name="close" size={18} />
        </button>
      </header>}
      {children}
    </section>
  </>;
  return typeof document === "undefined" ? content : createPortal(content, document.body);
}
