import { useState, type ReactNode } from "react";
import { Icon } from "../icons";
import { CenteredSheet } from "./CenteredSheet";

export function ChoicePicker({ label, value, options, onChange, disabled = false,
  className = "", children }: {
  label: string;
  value: string;
  options: { value: string; label: string; description?: string; icon?: string; disabled?: boolean }[];
  onChange: (value: string) => void;
  disabled?: boolean;
  className?: string;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(false);
  return <>
    <button type="button" className={`choice-picker-trigger ${className}`} disabled={disabled}
      aria-label={label} aria-haspopup="dialog" aria-expanded={open && !disabled}
      onClick={() => setOpen(!open)}>
      {children}<Icon name="chev" size={14} />
    </button>
    <CenteredSheet open={open && !disabled} label={label} onClose={() => setOpen(false)} maxWidth={440}>
      <div className="sheet-scroll" onKeyDown={event => {
        if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
        event.preventDefault();
        const items = [...event.currentTarget.querySelectorAll<HTMLButtonElement>("button:not(:disabled)")];
        const index = items.indexOf(document.activeElement as HTMLButtonElement);
        const next = event.key === "Home" ? 0 : event.key === "End" ? items.length - 1
          : (index + (event.key === "ArrowDown" ? 1 : -1) + items.length) % items.length;
        items[next]?.focus();
      }}>
        {options.map(option => <button key={option.value} type="button"
          className={`cmd${option.value === value ? " sel" : ""}`}
          aria-pressed={option.value === value} disabled={option.disabled}
          onClick={() => { onChange(option.value); setOpen(false); }}>
          {option.icon && <span className="cmd-ic"><Icon name={option.icon} size={18} /></span>}
          <span className="cmd-tx"><span className="cmd-nm">{option.label}</span>
            {option.description && <span className="cmd-ds">{option.description}</span>}
          </span>
          {option.value === value && <span className="cmd-check"><Icon name="check" size={18} /></span>}
        </button>)}
      </div>
    </CenteredSheet>
  </>;
}
