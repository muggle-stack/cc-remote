import { useLayoutEffect, useRef, useState } from "react";
import { sidebarDragIntent, sidebarReleaseOpen } from "./responsive-layout";
import { sidebarOpenFeedback } from "./sidebar-feedback";

const MOBILE = "(max-width: 979px)";
const LOCKED_TARGET = "[data-lock-horizontal-swipe], input, textarea, select, "
  + "[contenteditable]:not([contenteditable=false]), [role=slider], [role=dialog], [role=menu], [role=listbox], pre";

interface Drag {
  id: number;
  x: number;
  y: number;
  base: number;
  offset: number;
  width: number;
  wasOpen: boolean;
  claimed: boolean;
  samples: { x: number; time: number }[];
}

/** Move compositor layers, not React's streaming conversation, on each touch. */
export function useMobileSidebar(open: boolean, onOpenChange?: (open: boolean) => void) {
  const [sidebar, ref] = useState<HTMLElement | null>(null);
  const openRef = useRef(open);
  const syncRef = useRef<(() => void) | null>(null);
  useLayoutEffect(() => {
    openRef.current = open;
    syncRef.current?.();
  }, [open]);

  useLayoutEffect(() => {
    const shell = sidebar?.closest<HTMLElement>(".shell");
    if (!shell || !onOpenChange) return;
    const media = window.matchMedia(MOBILE);
    let viewportWidth = window.innerWidth;
    let drag: Drag | null = null;
    let frame = 0;
    let timer = 0;
    let expectedOpen: boolean | null = null;
    let suppressClickUntil = 0;
    const pane = () => shell.querySelector<HTMLElement>(":scope > .pane");
    const write = (offset: number, width: number) => {
      shell.style.setProperty("--sidebar-x", `${offset}px`);
      shell.style.setProperty("--sidebar-progress", `${offset / width}`);
    };
    const clearMotion = () => {
      cancelAnimationFrame(frame);
      clearTimeout(timer);
      frame = timer = 0;
      drag = null;
      expectedOpen = null;
      delete shell.dataset.sidebarMotion;
      shell.style.removeProperty("--sidebar-x");
      shell.style.removeProperty("--sidebar-progress");
    };
    const syncFocus = () => {
      const page = pane();
      const active = document.activeElement;
      if (media.matches && openRef.current && active instanceof HTMLElement
          && page?.contains(active)) active.blur();
      // The controller lives with the lazy sidebar; only it owns pane inertness.
      if (page) page.inert = media.matches && openRef.current;
    };
    syncRef.current = () => {
      if (expectedOpen === openRef.current) expectedOpen = null;
      else clearMotion();
      syncFocus();
    };
    const settle = (next: boolean, gesture: Drag) => {
      cancelAnimationFrame(frame);
      clearTimeout(timer);
      // Flush the final finger position before enabling the settle transition.
      write(gesture.offset, gesture.width);
      pane()?.getBoundingClientRect();
      drag = null;
      frame = 0;
      expectedOpen = next;
      shell.dataset.sidebarMotion = "settling";
      write(next ? gesture.width : 0, gesture.width);
      onOpenChange(next);
      // Also handles reduced motion and canceled/missing transitionend events.
      timer = window.setTimeout(clearMotion,
        window.matchMedia("(prefers-reduced-motion: reduce)").matches ? 0 : 360);
    };
    const cancel = () => {
      if (drag?.claimed) settle(drag.wasOpen, drag);
      else drag = null;
    };
    const locked = (target: Element) => {
      if (target.closest(LOCKED_TARGET) || window.getSelection()?.isCollapsed === false) return true;
      // Preserve nested horizontal scrollers, including account filters/tables.
      for (let node: Element | null = target; node && node !== shell; node = node.parentElement) {
        if (node.scrollWidth > node.clientWidth + 2
            && /auto|scroll/.test(getComputedStyle(node).overflowX)) return true;
      }
      return false;
    };
    const start = (event: TouchEvent) => {
      if (event.touches.length !== 1) { cancel(); return; }
      suppressClickUntil = 0;
      if (!media.matches || !(event.target instanceof Element)
          || !event.target.closest(".pane, .sessions, .scrim-side") || locked(event.target)) return;
      const touch = event.touches[0];
      if (!openRef.current && touch.clientX > window.innerWidth / 3) return;
      const width = shell.querySelector<HTMLElement>(":scope > .sessions")?.getBoundingClientRect().width;
      const page = pane();
      if (!width || !page) return;
      const base = Math.max(0, Math.min(width,
        page.getBoundingClientRect().left - shell.getBoundingClientRect().left));
      drag = { id: touch.identifier, x: touch.clientX, y: touch.clientY,
        base, offset: base, width, wasOpen: openRef.current, claimed: false,
        samples: [{ x: touch.clientX, time: event.timeStamp }] };
    };
    const move = (event: TouchEvent) => {
      if (!drag) return;
      if (event.touches.length !== 1) { cancel(); return; }
      const touch = event.touches[0];
      if (touch.identifier !== drag.id) return;
      const dx = touch.clientX - drag.x;
      if (!drag.claimed) {
        const intent = sidebarDragIntent(dx, touch.clientY - drag.y);
        if (intent === "pending") return;
        if (intent === "vertical" || (!drag.wasOpen && drag.base === 0 && dx < 0)) {
          drag = null; return;
        }
        // Once native scrolling owns the touch, do not turn it into navigation.
        if (!event.cancelable || window.getSelection()?.isCollapsed === false) { drag = null; return; }
        clearTimeout(timer);
        timer = 0;
        shell.dataset.sidebarMotion = "dragging";
        drag.claimed = true;
      }
      if (event.cancelable) event.preventDefault();
      drag.offset = Math.max(0, Math.min(drag.width, drag.base + dx));
      drag.samples = drag.samples.filter(sample => event.timeStamp - sample.time <= 100);
      drag.samples.push({ x: touch.clientX, time: event.timeStamp });
      if (!frame) frame = requestAnimationFrame(() => {
        frame = 0;
        if (drag?.claimed) write(drag.offset, drag.width);
      });
    };
    const end = (event: TouchEvent) => {
      if (!drag) return;
      const gesture = drag;
      const touch = Array.from(event.changedTouches).find(value => value.identifier === gesture.id);
      if (!touch) return;
      if (!gesture.claimed) { drag = null; return; }
      if (event.cancelable) event.preventDefault();
      suppressClickUntil = performance.now() + 400;
      gesture.offset = Math.max(0, Math.min(gesture.width, gesture.base + touch.clientX - gesture.x));
      const sample = gesture.samples.find(value => event.timeStamp - value.time <= 100);
      const elapsed = sample ? event.timeStamp - sample.time : 0;
      const velocity = sample && elapsed > 0 ? (touch.clientX - sample.x) / elapsed : 0;
      const next = sidebarReleaseOpen(gesture.offset, gesture.width, velocity);
      if (next && !gesture.wasOpen) sidebarOpenFeedback();
      settle(next, gesture);
    };
    const click = (event: MouseEvent) => {
      if (event.detail && performance.now() < suppressClickUntil) {
        event.preventDefault(); event.stopImmediatePropagation(); suppressClickUntil = 0;
      }
    };
    const resize = () => {
      syncFocus();
      // Keyboard/visual viewport height changes must not reset a horizontal drag.
      if (viewportWidth !== window.innerWidth) {
        viewportWidth = window.innerWidth;
        clearMotion();
      }
    };
    syncFocus();
    shell.addEventListener("touchstart", start, { passive: true });
    shell.addEventListener("touchmove", move, { passive: false });
    shell.addEventListener("touchend", end, { passive: false });
    shell.addEventListener("touchcancel", cancel);
    shell.addEventListener("click", click, true);
    window.addEventListener("resize", resize);
    media.addEventListener("change", resize);
    return () => {
      clearMotion(); syncRef.current = null;
      const page = pane();
      if (page) page.inert = false;
      shell.removeEventListener("touchstart", start);
      shell.removeEventListener("touchmove", move);
      shell.removeEventListener("touchend", end);
      shell.removeEventListener("touchcancel", cancel);
      shell.removeEventListener("click", click, true);
      window.removeEventListener("resize", resize);
      media.removeEventListener("change", resize);
    };
  }, [sidebar, onOpenChange]);
  return ref;
}
