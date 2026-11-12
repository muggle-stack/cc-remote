import {
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type ReactNode,
  type RefObject,
} from "react";
import { createPortal } from "react-dom";
import { useAnchoredPopoverGeometry } from "../chat-dialog-geometry";
import { Icon } from "../icons";

export interface CollaborationChoice {
  value: string;
  label: string;
  description?: string;
  group?: string;
  icon?: ReactNode;
  disabled?: boolean;
  danger?: boolean;
}

function SelectionMenu({ id, label, value, options, anchor, onSelect, onClose, actions }: {
  id: string;
  label: string;
  value: string;
  options: CollaborationChoice[];
  anchor: RefObject<HTMLButtonElement | null>;
  onSelect: (value: string) => void;
  onClose: (restoreFocus: boolean) => void;
  actions: boolean;
}) {
  const menu = useRef<HTMLDivElement>(null);
  const geometry = useAnchoredPopoverGeometry({
    open: true, anchorRef: anchor, maxWidth: 300, maxHeight: 380,
    minimumHeight: 180, align: "start",
  });
  const ready = !!geometry;
  useLayoutEffect(() => {
    if (!ready) return;
    const selected = menu.current?.querySelector<HTMLButtonElement>('[aria-checked="true"]:not(:disabled)')
      ?? menu.current?.querySelector<HTMLButtonElement>('button:not(:disabled)')
      ?? menu.current;
    selected?.focus({ preventScroll: true });
    selected?.scrollIntoView({ block: "nearest" });
  }, [ready]);
  useEffect(() => {
    const outside = (event: PointerEvent | FocusEvent) => {
      if (event.target instanceof Node && !menu.current?.contains(event.target)
          && !anchor.current?.contains(event.target)) onClose(false);
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || event.isComposing || event.keyCode === 229) return;
      // Consume Escape before the native dialog's cancel closes its owner.
      event.preventDefault();
      event.stopPropagation();
      onClose(true);
    };
    document.addEventListener("pointerdown", outside, true);
    document.addEventListener("focusin", outside);
    document.addEventListener("keydown", escape, true);
    return () => {
      document.removeEventListener("pointerdown", outside, true);
      document.removeEventListener("focusin", outside);
      document.removeEventListener("keydown", escape, true);
    };
  }, [anchor, onClose]);
  // A native modal's selectors must stay in its top layer and focus boundary.
  const host = anchor.current?.closest("dialog") ?? document.body;
  return createPortal(<div ref={menu} id={id} className="collab-select-popover" role="menu"
    aria-label={label} tabIndex={-1} style={geometry ? {
      left: geometry.left, top: geometry.top, width: geometry.width,
      maxHeight: geometry.maxHeight,
      transform: `translate(-50%, ${geometry.placement === "above" ? "-100%" : "0"})`,
    } : { visibility: "hidden" }}
    onKeyDown={event => {
      if (event.nativeEvent.isComposing || event.keyCode === 229) return;
      if (event.key === "Tab") {
        const scope = anchor.current?.closest("dialog, [data-collab-editor]");
        if (!scope) {
          onClose(true);
          return;
        }
        // Native Safari may skip buttons in both sheets and inline editors.
        event.preventDefault();
        event.stopPropagation();
        const items = [...scope.querySelectorAll<HTMLElement>(
          'button:not(:disabled), input:not(:disabled), textarea:not(:disabled), select:not(:disabled), a[href], [tabindex="0"]',
        )].filter(node => node.tabIndex >= 0 && node.getClientRects().length > 0);
        const index = items.indexOf(anchor.current!);
        const next = (index + (event.shiftKey ? -1 : 1) + items.length) % items.length;
        onClose(false);
        items[next]?.focus({ preventScroll: true });
        return;
      }
      if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      event.stopPropagation();
      const items = [...event.currentTarget.querySelectorAll<HTMLButtonElement>('button:not(:disabled)')];
      if (!items.length) return;
      const index = items.indexOf(document.activeElement as HTMLButtonElement);
      const next = event.key === "Home" ? 0 : event.key === "End" ? items.length - 1
        : (index + (event.key === "ArrowDown" ? 1 : -1) + items.length) % items.length;
      items[next]?.focus({ preventScroll: true });
      items[next]?.scrollIntoView({ block: "nearest" });
    }}>
    <div className="collab-select-title">{label}</div>
    {options.map((option, index) => <div key={option.value} role="none">
      {option.group && option.group !== options[index - 1]?.group
        && <div className="collab-select-group" role="presentation">{option.group}</div>}
      <button type="button" role={actions ? "menuitem" : "menuitemradio"} aria-label={option.label}
        aria-checked={actions ? undefined : option.value === value}
        className={option.danger ? "danger" : undefined} disabled={option.disabled} tabIndex={-1}
        onClick={() => { onSelect(option.value); onClose(true); }}>
        {option.icon}
        <span className="collab-select-copy"><span>{option.label}</span>
          {option.description && <small>{option.description}</small>}</span>
      </button>
    </div>)}
    {!options.length && <p className="collab-muted" role="status">暂无可选项</p>}
  </div>, host);
}

export function CollaborationSelect({ label, value, options, onChange, children,
  disabled = false, className = "", actions = false, showChevron = true }: {
  label: string;
  value: string;
  options: CollaborationChoice[];
  onChange: (value: string) => void;
  children?: ReactNode;
  disabled?: boolean;
  className?: string;
  actions?: boolean;
  showChevron?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const anchor = useRef<HTMLButtonElement>(null);
  const id = useId();
  const close = useCallback((restoreFocus: boolean) => {
    setOpen(false);
    if (restoreFocus) anchor.current?.focus({ preventScroll: true });
  }, []);
  return <>
    <button ref={anchor} type="button" className={`collab-select-trigger ${className}`}
      aria-label={label} aria-haspopup="menu" aria-expanded={open && !disabled}
      aria-controls={open && !disabled ? id : undefined} disabled={disabled}
      onClick={() => setOpen(!open)} onKeyDown={event => {
        if (event.key === "ArrowDown" || event.key === "ArrowUp") {
          event.preventDefault();
          setOpen(true);
        }
      }}>
      {children ?? <span>{options.find(option => option.value === value)?.label || value || "默认"}</span>}
      {showChevron && <Icon name="chev" size={14} />}
    </button>
    {open && !disabled && <SelectionMenu id={id} label={label} value={value}
      options={options} anchor={anchor} onSelect={onChange} onClose={close} actions={actions} />}
  </>;
}
