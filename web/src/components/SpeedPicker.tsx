import { lazy, Suspense, useCallback, useEffect, useRef, useState } from "react";
import type { Catalog } from "../data";
import { normalizeSpeed, speedLabel } from "../codex-speed-label";
const SpeedPopover = lazy(() => import("./SpeedPopover"));

export interface SpeedPickerProps {
  model: string | null | undefined;
  catalog?: Catalog;
  value: string | null | undefined;
  disabled?: boolean;
  newSession?: boolean;
  row?: boolean;
  scopeKey?: string;
  onRefresh?: () => void;
  onChange: (tier: string) => void;
}

export function SpeedPicker(p: SpeedPickerProps) {
  const [open, setOpen] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);
  const options = p.catalog?.codex?.find((model) => model.id === p.model)?.service_tiers ?? [];
  const selected = p.value == null ? null : normalizeSpeed(p.value);
  const label = speedLabel(selected, options);
  const close = useCallback(() => setOpen(false), []);
  useEffect(() => { setOpen(false); }, [p.scopeKey, p.model, p.disabled]);
  return <>
    <button type="button" ref={trigger}
      className={p.row ? "work-fast-setting" : `hint-ctl fast-chip${selected && selected !== "default" ? " on" : ""}`}
      aria-label={`速度：${label}`} aria-haspopup="menu" aria-expanded={open}
      disabled={p.disabled}
      title="选择当前模型的速度"
      onClick={() => { if (!open) p.onRefresh?.(); setOpen(!open); }}>
      {p.row ? <><span>速度</span><b>{label}</b></> : label}
    </button>
    {open && <Suspense fallback={null}>
      <SpeedPopover {...p} trigger={trigger} onClose={close} />
    </Suspense>}
  </>;
}
