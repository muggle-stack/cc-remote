import { useEffect, useId, useRef, useState } from "react";
import { Icon } from "../icons";
import type { DshPanelSelection } from "./DshPanel";

type Kind = DshPanelSelection["kind"];
import { dshToolLabels } from "../dsh-tool-labels";
const groups: { label: string; entries: [Kind, string][] }[] = [
  { label: "会话与任务", entries: [["subagents", "user"], ["jobs", "queue"], ["workflows", "branch"], ["trajectory", "clock"], ["stats", "sliders"], ["deliverables", "read"], ["schedules", "calendar"]] },
  { label: "配置", entries: [["settings", "sliders"], ["bundles", "archive"], ["presets", "plan"], ["terminals", "bash"], ["plugins", "code"], ["diagnostics", "devices"]] },
];

export function DshToolsMenu({ selected, onSelect }: { selected: Kind; onSelect: (kind: Kind) => void }) {
  const [open, setOpen] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  const id = useId();
  useEffect(() => {
    if (!open) return;
    const root = menu.current!;
    (root.querySelector<HTMLButtonElement>('[aria-current="page"]') ?? root.querySelector<HTMLButtonElement>("button"))?.focus();
    const outside = (event: PointerEvent) => {
      if (!root.contains(event.target as Node) && !trigger.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", outside);
    return () => document.removeEventListener("pointerdown", outside);
  }, [open]);
  const dismiss = () => { setOpen(false); trigger.current?.focus(); };
  return <>
    <button className="dsh-tools-trigger" ref={trigger} aria-haspopup="menu" aria-expanded={open} aria-controls={open ? id : undefined} onClick={() => setOpen(value => !value)}>
      <svg width="15" height="15" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden="true"><rect x="2" y="2" width="6" height="6" rx="1" /><rect x="12" y="2" width="6" height="6" rx="1" /><rect x="2" y="12" width="6" height="6" rx="1" /><rect x="12" y="12" width="6" height="6" rx="1" /></svg>工具
    </button>
    {open && <div className="dsh-tools-menu" id={id} ref={menu} role="menu" aria-label="会话工具" onBlur={event => {
      // Safari may blur to the body on touch before the item's click arrives.
      // Pointer dismissal is handled separately; only dismiss a known focus move.
      if (event.relatedTarget && !event.currentTarget.contains(event.relatedTarget as Node) && event.relatedTarget !== trigger.current) setOpen(false);
    }} onKeyDown={event => {
      if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); dismiss(); return; }
      const buttons = Array.from(menu.current!.querySelectorAll<HTMLButtonElement>("button"));
      const index = buttons.indexOf(document.activeElement as HTMLButtonElement);
      const step = event.key === "ArrowDown" || event.key === "ArrowRight" ? 1 : event.key === "ArrowUp" || event.key === "ArrowLeft" ? -1 : 0;
      if (step || event.key === "Home" || event.key === "End") {
        event.preventDefault(); buttons[event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1 : (index + step + buttons.length) % buttons.length]?.focus();
      }
    }}>
      {groups.map(group => <div key={group.label} role="group" aria-label={group.label}>
        <small>{group.label}</small><div>{group.entries.map(([kind, icon]) => <button key={kind} role="menuitem" aria-current={selected === kind ? "page" : undefined} onClick={() => { dismiss(); onSelect(kind); }}><Icon name={icon} size={17} />{dshToolLabels[kind]}</button>)}</div>
      </div>)}
    </div>}
  </>;
}
