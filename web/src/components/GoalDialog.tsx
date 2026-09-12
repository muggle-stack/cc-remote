import { useEffect, useId, useRef, useState, type ReactNode, type RefObject } from "react";
import { useChatDialogGeometry } from "../chat-dialog-geometry";
import { EngineIcon, Icon } from "../icons";
import type { Engine } from "../protocol";
import "./GoalDialog.css";
import { goalTokens } from "../goal-presentation";

export function GoalSheet({ engine, title, status, scopeRef, onClose, children, footer }: {
  engine: Engine; title: string; status?: string; scopeRef: RefObject<HTMLElement | null>;
  onClose: () => void; children: ReactNode; footer: ReactNode;
}) {
  const dialogRef = useRef<HTMLElement>(null);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  const geometry = useChatDialogGeometry({
    open: true, maxWidth: 540, maxHeight: 720, minimumHeight: 320, scopeRef,
  });
  const hasGeometry = !!geometry;
  useEffect(() => {
    if (!hasGeometry) return;
    const previous = document.activeElement as HTMLElement | null;
    // Focus the card, without summoning the mobile keyboard on a read.
    dialogRef.current?.focus({ preventScroll: true });
    const onKey = (event: KeyboardEvent) => {
      if (event.isComposing || event.keyCode === 229) return;
      if (event.key === "Escape" && !event.defaultPrevented) {
        event.preventDefault(); closeRef.current();
      }
      if (event.key !== "Tab") return;
      const nodes = [...(dialogRef.current?.querySelectorAll<HTMLElement>(
        'button:not(:disabled), input:not(:disabled), textarea:not(:disabled), [tabindex="0"]',
      ) ?? [])].filter(node => node.getClientRects().length);
      const target = event.shiftKey ? nodes.at(-1) : nodes[0];
      if (!nodes.includes(document.activeElement as HTMLElement)
          || document.activeElement === (event.shiftKey ? nodes[0] : nodes.at(-1))) {
        event.preventDefault(); target?.focus();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      if (previous?.isConnected) previous.focus({ preventScroll: true });
    };
  // Geometry updates during keyboard/layout changes must not steal input focus.
  }, [hasGeometry]);
  if (!geometry) return null;
  const name = engine === "codex" ? "Codex" : "Claude";
  return <>
    <div className="scrim show goal-scrim" onClick={onClose} />
    <section ref={dialogRef} tabIndex={-1} className={`sheet show goal-sheet goal-card goal-card-${engine}`}
      role="dialog" aria-modal="true" aria-label={`${name} Goal`} style={geometry}>
      <header className="goal-sheet-head">
        <span className="goal-sheet-icon"><EngineIcon engine={engine} size={23} /></span>
        <b>{title}</b>
        {status && <span className="goal-card-status"><i />{status}</span>}
        <button type="button" className="goal-icon-button" onClick={onClose} aria-label="关闭">
          <Icon name="close" size={18} />
        </button>
      </header>
      <div className="goal-sheet-scroll">{children}</div>
      <footer className="goal-sheet-actions">{footer}</footer>
    </section>
  </>;
}

export function GoalObjective({ value, onChange, hint, disabled = false }: {
  value: string; onChange: (value: string) => void; hint: string; disabled?: boolean;
}) {
  return <div className="goal-editor">
    <textarea aria-label="目标内容" value={value} disabled={disabled} maxLength={16384}
      placeholder="这次，你想完成什么？" rows={4}
      onChange={event => onChange(event.target.value)} />
    <p className="goal-hint">{hint}</p>
  </div>;
}

export function GoalLimit({ value, onChange, allowUnlimited = true, disabled = false }: {
  value: string; onChange: (value: string) => void;
  allowUnlimited?: boolean; disabled?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const id = useId();
  const label = "Token 预算";
  const valid = value === "" ? allowUnlimited : /^[0-9]+$/.test(value)
    && Number.isSafeInteger(Number(value)) && Number(value) > 0;
  const presets = [50000, 100000, 250000, 500000];
  return <div className="goal-limit">
    <button type="button" className="goal-limit-trigger" disabled={disabled} aria-expanded={open}
      aria-controls={id} onClick={() => setOpen(!open)}>
      <Icon name="cpu" size={16} />
      预算 · {!valid ? "自定义" : value ? goalTokens(Number(value)) : "不限"}
      <Icon name="chev" size={14} />
    </button>
    {open && <div className="goal-limit-picker" id={id} onKeyDown={event => {
      if (event.key === "Escape" && !event.nativeEvent.isComposing) {
        event.preventDefault(); event.stopPropagation(); setOpen(false);
      }
    }}>
      <label>{label}<input aria-label={label} type="text" inputMode="numeric" value={value}
        aria-invalid={!valid} placeholder="不限"
        onChange={event => onChange(event.target.value)} /></label>
      <div className="goal-limit-presets">
        {allowUnlimited && <button type="button" onClick={() => { onChange(""); setOpen(false); }}>不限</button>}
        {presets.map(n => <button type="button" key={n} onClick={() => { onChange(String(n)); setOpen(false); }}>
          {goalTokens(n)}
        </button>)}
      </div>
      {!valid && <small role="alert">请输入大于 0 的整数</small>}
      <button type="button" className="goal-limit-done" disabled={!valid} onClick={() => setOpen(false)}>确定</button>
    </div>}
  </div>;
}

export function GoalMeter({ label, used, total }: {
  label: string; used: number; total: number;
}) {
  return <div className="goal-budget">
    <div><span>{label}</span><b>{goalTokens(used)} <em>/ {goalTokens(total)}</em></b></div>
    <progress value={used} max={total} aria-label={label} />
  </div>;
}

export function GoalMore({ disabled, children }: { disabled?: boolean; children: ReactNode }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const close = (event: PointerEvent) => {
      if (!ref.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", close);
    return () => document.removeEventListener("pointerdown", close);
  }, [open]);
  return <div ref={ref} className="goal-more" onKeyDown={event => {
    if (open && event.key === "Escape" && !event.nativeEvent.isComposing) {
      event.preventDefault(); event.stopPropagation(); setOpen(false);
    }
  }}>
    <button className="goal-icon-button" type="button" aria-label="更多目标操作" aria-expanded={open}
      disabled={disabled} onClick={() => setOpen(!open)}><Icon name="dots" size={18} /></button>
    {open && <div className="goal-more-menu" onClick={() => setOpen(false)}>{children}</div>}
  </div>;
}
