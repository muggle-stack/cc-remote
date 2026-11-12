import { lazy, Suspense, useEffect, useRef, useState } from "react";
import type { Engine } from "../protocol";
import { EngineIcon, Icon } from "../icons";

const EngineMenu = lazy(() => import("./EngineMenu"));

export function EngineSelector({ engine, onChange, onCollaboration }: {
  engine: Engine | "collaboration";
  onChange: (engine: Engine) => void;
  onCollaboration?: () => void;
}) {
  const button = useRef<HTMLButtonElement>(null);
  const [menu, setMenu] = useState<boolean | null>(null);
  const altShortcuts = typeof navigator !== "undefined"
    && navigator.platform.startsWith("Linux")
    && !/Android|CrOS/.test(navigator.userAgent);
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.isComposing || event.keyCode === 229 || event.repeat
          || event.shiftKey
          || !["1", "2", "3", "4"].includes(event.key)) return;
      if (altShortcuts
          ? !event.altKey || event.ctrlKey || event.metaKey
          : !(event.metaKey || event.ctrlKey) || event.altKey) return;
      if (event.key === "4" && !onCollaboration && engine !== "collaboration") return;
      // Harness navigation belongs to the app, even inside a modal or editor.
      // Capture before their key handlers; never submit or dismiss their work.
      event.preventDefault();
      event.stopPropagation();
      setMenu(null);
      if (event.key === "4") {
        if (engine !== "collaboration") onCollaboration?.();
        return;
      }
      const next = event.key === "1" ? "claude" : event.key === "2" ? "codex" : "dsh";
      if (next !== engine) onChange(next);
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [engine, onChange, onCollaboration, altShortcuts]);
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
      <span className="engine-label">{engine === "collaboration"
        ? <Icon name="collaboration" /> : <EngineIcon engine={engine} />}
        <span className="engine-name">{engine === "collaboration" ? "协作"
          : engine === "dsh" ? "DSH" : engine === "codex" ? "Codex" : "Claude"}</span></span>
    </button>
    {menu !== null && <Suspense fallback={null}><EngineMenu engine={engine} anchor={button}
      onCollaboration={onCollaboration || engine === "collaboration"
        ? keyboard => { close(keyboard); onCollaboration?.(); } : undefined}
      keyboard={menu} altShortcuts={altShortcuts} onClose={close} onSelect={(next, keyboard) => {
        close(keyboard);
        if (next !== engine) onChange(next);
      }} /></Suspense>}
  </span>;
}
