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
  interrupted: boolean;
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
    if (!sidebar || !shell || !onOpenChange) return;
    const media = window.matchMedia(MOBILE);
    const page = shell.querySelector<HTMLElement>(":scope > .pane");
    const scrim = shell.querySelector<HTMLElement>(":scope > .scrim-side");
    if (!page || !scrim) return;
    let viewportWidth = window.innerWidth;
    let drag: Drag | null = null;
    let frame = 0;
    let timer = 0;
    let expectedOpen: boolean | null = null;
    let settleOffset = 0;
    let suppressClickUntil = 0;
    const write = (offset: number, width: number) => {
      // Non-inherited properties invalidate these three layers only. Shell-level
      // custom properties used to invalidate the conversation on every frame.
      page.style.transform = `translate3d(${offset}px,0,0)`;
      sidebar.style.transform = `translate3d(${(offset - width) * .22}px,0,0)`;
      scrim.style.transform = `translate3d(${offset}px,0,0)`;
      scrim.style.opacity = `${offset / width}`;
    };
    const clearMotion = () => {
      cancelAnimationFrame(frame);
      clearTimeout(timer);
      frame = timer = 0;
      drag = null;
      expectedOpen = null;
      delete shell.dataset.sidebarMotion;
      page.style.removeProperty("transform");
      sidebar.style.removeProperty("transform");
      scrim.style.removeProperty("transform");
      scrim.style.removeProperty("opacity");
    };
    const syncFocus = () => {
      const active = document.activeElement;
      if (media.matches && openRef.current && active instanceof HTMLElement
          && page.contains(active)) active.blur();
      // The controller lives with the lazy sidebar; only it owns pane inertness.
      page.inert = media.matches && openRef.current;
    };
    const finishLater = () => {
      // transitionend normally finishes first; this covers reduced motion and
      // missing/canceled events without leaving an untouchable closing layer.
      timer = window.setTimeout(clearMotion,
        window.matchMedia("(prefers-reduced-motion: reduce)").matches ? 0 : 320);
    };
    const settle = (next: boolean, gesture: Drag, commit = true) => {
      cancelAnimationFrame(frame);
      clearTimeout(timer);
      // Flush the final finger position before enabling the settle transition.
      write(gesture.offset, gesture.width);
      void getComputedStyle(page).transform;
      drag = null;
      frame = 0;
      expectedOpen = commit ? next : null;
      settleOffset = next ? gesture.width : 0;
      shell.dataset.sidebarMotion = "settling";
      write(settleOffset, gesture.width);
      if (commit) onOpenChange(next);
      finishLater();
    };
    syncRef.current = () => {
      if (expectedOpen === openRef.current) expectedOpen = null;
      else {
        clearMotion();
        // Button/selection changes also keep effects alive through the closing
        // transition, so the next swipe can grab it just like a gesture settle.
        if (media.matches) {
          settleOffset = openRef.current ? sidebar.getBoundingClientRect().width : 0;
          shell.dataset.sidebarMotion = "settling";
          finishLater();
        }
      }
      syncFocus();
    };
    const abandon = () => {
      if (drag?.interrupted) settle(drag.wasOpen, drag, false);
      else drag = null;
    };
    const cancel = () => {
      if (drag?.claimed) settle(drag.wasOpen, drag);
      else abandon();
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
          || (event.target !== shell && !event.target.closest(".pane, .sessions, .scrim-side"))
          || locked(event.target)) return;
      const touch = event.touches[0];
      const width = sidebar.getBoundingClientRect().width;
      if (!width) return;
      const base = Math.max(0, Math.min(width,
        page.getBoundingClientRect().left - shell.getBoundingClientRect().left));
      const interrupted = shell.dataset.sidebarMotion === "settling";
      // A new press owns the previous animation immediately, even before its
      // direction is known. Its old cleanup must never discard this touch.
      cancelAnimationFrame(frame);
      clearTimeout(timer);
      frame = timer = 0;
      if (interrupted) {
        shell.dataset.sidebarMotion = "holding";
        write(base, width);
      }
      drag = { id: touch.identifier, x: touch.clientX, y: touch.clientY,
        base, offset: base, width, wasOpen: openRef.current, claimed: false,
        interrupted,
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
          abandon(); return;
        }
        // Once native scrolling owns the touch, do not turn it into navigation.
        if (!event.cancelable || window.getSelection()?.isCollapsed === false) { abandon(); return; }
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
      if (!gesture.claimed) { abandon(); return; }
      if (event.cancelable) event.preventDefault();
      suppressClickUntil = performance.now() + 400;
      gesture.offset = Math.max(0, Math.min(gesture.width, gesture.base + touch.clientX - gesture.x));
      // Touchend can be delivered a frame or two after the last move. Repeating
      // its unchanged coordinate must not dilute an otherwise deliberate flick.
      // A genuine hold beyond the recent-motion window still settles by distance.
      const last = gesture.samples.at(-1);
      const final = last?.x === touch.clientX ? last : { x: touch.clientX, time: event.timeStamp };
      const sample = event.timeStamp - final.time <= 100
        ? gesture.samples.find(value => final.time - value.time <= 100 && value.time < final.time)
        : undefined;
      const elapsed = sample ? final.time - sample.time : 0;
      const velocity = sample && elapsed > 0 ? (final.x - sample.x) / elapsed : 0;
      const next = sidebarReleaseOpen(gesture.offset, gesture.width, velocity);
      if (next && !gesture.wasOpen) sidebarOpenFeedback();
      settle(next, gesture);
    };
    const click = (event: MouseEvent) => {
      if (event.detail && performance.now() < suppressClickUntil) {
        event.preventDefault(); event.stopImmediatePropagation(); suppressClickUntil = 0;
      }
    };
    const pointerDown = (event: PointerEvent) => {
      // A separate mouse/pen press is not the compatibility click of a touch
      // drag. Genuine new touch presses reset suppression in start() above.
      if (event.pointerType !== "touch") suppressClickUntil = 0;
    };
    const transitionEnd = (event: TransitionEvent) => {
      if (event.target === page && event.propertyName === "transform"
          && !drag && shell.dataset.sidebarMotion === "settling") {
        // An earlier transition's queued event must not finish its replacement.
        const transform = getComputedStyle(page).transform;
        const offset = transform === "none" ? 0 : new DOMMatrixReadOnly(transform).m41;
        if (Math.abs(offset - settleOffset) < 1) clearMotion();
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
    shell.addEventListener("pointerdown", pointerDown, true);
    shell.addEventListener("transitionend", transitionEnd);
    window.addEventListener("resize", resize);
    media.addEventListener("change", resize);
    return () => {
      clearMotion(); syncRef.current = null;
      page.inert = false;
      shell.removeEventListener("touchstart", start);
      shell.removeEventListener("touchmove", move);
      shell.removeEventListener("touchend", end);
      shell.removeEventListener("touchcancel", cancel);
      shell.removeEventListener("click", click, true);
      shell.removeEventListener("pointerdown", pointerDown, true);
      shell.removeEventListener("transitionend", transitionEnd);
      window.removeEventListener("resize", resize);
      media.removeEventListener("change", resize);
    };
  }, [sidebar, onOpenChange]);
  return ref;
}
