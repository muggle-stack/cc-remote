import { lazy, Suspense, useRef, useState } from "react";
import type { Engine } from "../protocol";
import { EngineIcon, Icon } from "../icons";

const EngineMenu = lazy(() => import("./EngineMenu"));

export function EngineSelector({ engine, onChange }: {
  engine: Engine;
  onChange: (engine: Engine) => void;
}) {
  const button = useRef<HTMLButtonElement>(null);
  const [menu, setMenu] = useState<boolean | null>(null);
  const close = (restoreFocus: boolean) => {
    setMenu(null);
    if (restoreFocus) button.current?.focus();
  };
  const open = (keyboard: boolean) => {
    if (!keyboard) button.current?.blur();
    setMenu(keyboard);
  };
  return <span className="engine-selector">
    <button ref={button} type="button" className="engine-toggle" aria-label="切换新会话引擎"
      aria-haspopup="menu" aria-expanded={menu !== null} title="切换会话引擎"
      onClick={event => menu !== null ? close(false) : open(event.detail === 0)}
      onKeyDown={event => {
        if (event.key === "ArrowDown" || event.key === "ArrowUp") {
          event.preventDefault(); open(true);
        }
      }}>
      <span className="engine-label"><EngineIcon engine={engine} />
        {engine === "dsh" ? "DSH" : engine === "codex" ? "Codex" : "Claude"}</span>
      <Icon name="chev" size={12} />
    </button>
    {menu !== null && <Suspense fallback={null}><EngineMenu engine={engine} anchor={button}
      keyboard={menu} onClose={close} onSelect={(next, keyboard) => {
        close(keyboard);
        if (next !== engine) onChange(next);
      }} /></Suspense>}
  </span>;
}
