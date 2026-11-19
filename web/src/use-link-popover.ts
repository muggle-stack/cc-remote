import { useCallback, useContext, useEffect, useId, useLayoutEffect, useRef, useState, type FocusEvent, type PointerEvent } from "react";
import { PanelVisible } from "./right-panel-presentation";

// A single owner spans messages, BTW, and file previews. Switching links also
// cancels the old owner's leave/open timers; it never waits for an exit animation.
let activePopover: (() => void) | null = null;

export function useLinkPopover(delay = 0) {
  const visible = useContext(PanelVisible);
  const id = useId();
  const anchor = useRef<HTMLSpanElement>(null);
  const panel = useRef<HTMLDivElement>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [shown, setShown] = useState(false);
  const [position, setPosition] = useState({ left: 0, top: 0 });
  const clear = useCallback(() => {
    if (timer.current !== null) clearTimeout(timer.current);
    timer.current = null;
  }, []);
  const close = useCallback(() => {
    clear(); setShown(false);
    if (activePopover === close) activePopover = null;
  }, [clear]);
  useEffect(() => { if (!visible) close(); }, [visible, close]);
  const open = (immediate = false) => {
    clear();
    if (activePopover !== close) activePopover?.();
    activePopover = close;
    if (immediate || shown || !delay) setShown(true);
    else timer.current = setTimeout(() => { timer.current = null; setShown(true); }, delay);
  };
  const leave = (event: PointerEvent | FocusEvent) => {
    clear();
    // The portal and its anchor are one hover/focus region. Loading a cover can
    // briefly grow the card over the pointer before it is repositioned. React
    // then emits a leave from the portal back to its ancestor anchor without
    // another enter on that anchor, so an unconditional timer would close it.
    const next = event.relatedTarget;
    if (next instanceof Node && (anchor.current?.contains(next) || panel.current?.contains(next))) return;
    timer.current = setTimeout(close, 180);
  };
  useEffect(() => () => { clear(); if (activePopover === close) activePopover = null; }, [clear, close]);
  useLayoutEffect(() => {
    if (!shown) return;
    const update = () => {
      if (!anchor.current || !panel.current) return;
      const a = anchor.current.getBoundingClientRect();
      const b = panel.current.getBoundingClientRect();
      const left = Math.max(10, Math.min(a.left + a.width / 2 - b.width / 2, window.innerWidth - b.width - 10));
      const top = Math.max(10, a.top - b.height - 8 >= 10 ? a.top - b.height - 8
        : Math.min(a.bottom + 8, window.innerHeight - b.height - 10));
      setPosition((old) => old.left === left && old.top === top ? old : { left, top });
    };
    update();
    const resize = new ResizeObserver(update);
    if (panel.current) resize.observe(panel.current);
    window.addEventListener("resize", update);
    window.addEventListener("scroll", update, true);
    const outside = (event: globalThis.PointerEvent) => {
      if (!anchor.current?.contains(event.target as Node) && !panel.current?.contains(event.target as Node)) close();
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault(); event.stopPropagation();
        if (panel.current?.contains(document.activeElement)) {
          anchor.current?.querySelector<HTMLButtonElement>("button")?.focus({ preventScroll: true });
        }
        close();
      }
    };
    document.addEventListener("pointerdown", outside, true);
    document.addEventListener("keydown", escape, true);
    return () => {
      resize.disconnect(); window.removeEventListener("resize", update);
      window.removeEventListener("scroll", update, true);
      document.removeEventListener("pointerdown", outside, true);
      document.removeEventListener("keydown", escape, true);
    };
  }, [shown, close]);
  const enter = (event: PointerEvent) => { if (event.pointerType !== "touch") open(); };
  return { id, anchor, panel, shown: visible && shown, position, open, close, enter, leave,
    keep: () => { clear(); }, focus: () => open(true) };
}
