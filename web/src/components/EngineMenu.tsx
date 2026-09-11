import { useEffect, useLayoutEffect, useRef, type RefObject } from "react";
import { createPortal } from "react-dom";
import type { Engine } from "../protocol";
import { EngineIcon, Icon } from "../icons";

const engines: Engine[] = ["claude", "codex", "dsh"];

export default function EngineMenu({ engine, anchor, keyboard, onClose, onSelect }: {
  engine: Engine;
  anchor: RefObject<HTMLButtonElement | null>;
  keyboard: boolean;
  onClose: (restoreFocus: boolean) => void;
  onSelect: (engine: Engine, keyboard: boolean) => void;
}) {
  const menu = useRef<HTMLDivElement>(null);
  const rect = anchor.current?.getBoundingClientRect();
  useLayoutEffect(() => {
    if (keyboard) menu.current?.querySelector<HTMLButtonElement>('[aria-checked="true"]')?.focus();
  }, [keyboard]);
  useEffect(() => {
    const outside = (event: PointerEvent) => {
      if (!menu.current?.contains(event.target as Node) && !anchor.current?.contains(event.target as Node)) onClose(false);
    };
    const resize = () => onClose(false);
    document.addEventListener("pointerdown", outside);
    window.addEventListener("resize", resize);
    return () => {
      document.removeEventListener("pointerdown", outside);
      window.removeEventListener("resize", resize);
    };
  }, [anchor, onClose]);
  return createPortal(<div ref={menu} className="engine-menu" role="menu" aria-label="会话引擎"
    style={{ top: (rect?.bottom ?? 0) + 8, right: Math.max(8, window.innerWidth - (rect?.right ?? 0)) }}
    onKeyDown={event => {
      if (event.key === "Escape" || event.key === "Tab") {
        if (event.key === "Escape") event.preventDefault();
        onClose(true);
        return;
      }
      const items = [...(menu.current?.querySelectorAll<HTMLButtonElement>('[role="menuitemradio"]') ?? [])];
      const index = items.indexOf(document.activeElement as HTMLButtonElement);
      let next: number;
      if (event.key === "ArrowDown") next = (index + 1) % items.length;
      else if (event.key === "ArrowUp") next = (index - 1 + items.length) % items.length;
      else if (event.key === "Home") next = 0;
      else if (event.key === "End") next = items.length - 1;
      else return;
      event.preventDefault();
      items[next]?.focus();
    }}>
    {engines.map(value => <button key={value} type="button" role="menuitemradio"
      aria-checked={value === engine} tabIndex={-1} className="engine-menu-item"
      onClick={event => onSelect(value, event.detail === 0)}>
      <EngineIcon engine={value} size={18} />
      <span>{value === "dsh" ? "DSH" : value === "codex" ? "Codex" : "Claude"}</span>
      {value === engine && <Icon name="check" size={16} />}
    </button>)}
  </div>, document.body);
}
