import { useEffect, useLayoutEffect, useRef, type RefObject } from "react";
import { createPortal } from "react-dom";
import type { Engine } from "../protocol";
import { EngineIcon, Icon } from "../icons";

const engines: Engine[] = ["claude", "codex", "dsh"];

export default function EngineMenu({ engine, anchor, keyboard, altShortcuts, onClose, onSelect }: {
  engine: Engine;
  anchor: RefObject<HTMLButtonElement | null>;
  keyboard: boolean;
  altShortcuts: boolean;
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
    const escape = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || event.isComposing || event.keyCode === 229) return;
      event.preventDefault();
      event.stopPropagation();
      onClose(true);
    };
    document.addEventListener("pointerdown", outside);
    document.addEventListener("keydown", escape, true);
    window.addEventListener("resize", resize);
    return () => {
      document.removeEventListener("pointerdown", outside);
      document.removeEventListener("keydown", escape, true);
      window.removeEventListener("resize", resize);
    };
  }, [anchor, onClose]);
  return createPortal(<div ref={menu} className="engine-menu" role="menu" aria-label="会话引擎"
    style={{ top: (rect?.bottom ?? 0) + 8, right: Math.max(8, window.innerWidth - (rect?.right ?? 0)) }}
    onKeyDown={event => {
      if (event.key === "Tab") {
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
      aria-keyshortcuts={altShortcuts ? `Alt+${engines.indexOf(value) + 1}`
        : `Meta+${engines.indexOf(value) + 1} Control+${engines.indexOf(value) + 1}`}
      onClick={event => onSelect(value, event.detail === 0)}>
      <EngineIcon engine={value} size={18} />
      <span>{value === "dsh" ? "DSH" : value === "codex" ? "Codex" : "Claude"}</span>
      {value === engine && <Icon name="check" size={16} />}
    </button>)}
  </div>, document.body);
}
