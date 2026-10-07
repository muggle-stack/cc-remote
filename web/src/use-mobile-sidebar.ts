import { useLayoutEffect, useRef, useState } from "react";
import { sidebarDragIntent } from "./responsive-layout";
import { sidebarOpenFeedback } from "./sidebar-feedback";

const MOBILE = "(max-width: 979px)";
const LOCKED_TARGET = "[data-lock-horizontal-swipe], input, textarea, select, "
  + "[contenteditable]:not([contenteditable=false]), [role=slider], [role=dialog], [role=menu], [role=listbox]";

function sidebarReleaseOpen(offset: number, width: number, velocity: number): boolean {
  // A recent flick carries the drawer; a held/slow drag settles by distance.
  if (Math.abs(velocity) >= 0.45) return velocity > 0;
  return offset >= width / 2;
}

function sidebarReleaseMotion(from: number, to: number, velocity: number) {
  const distance = Math.abs(to - from);
  if (distance < .1) return { duration: 0, easing: "linear" };
  const speed = Math.max(0, velocity * Math.sign(to - from));
  // Let a short flick keep travelling instead of jumping to the same fast
  // ease-out as a full-width swipe. Match its initial speed, then arrive at
  // rest without overshoot. Opposing/held gestures start their return at rest.
  let duration = Math.min(420, Math.max(160, 420 * Math.sqrt(distance / 320)));
  if (speed > 0) duration = Math.min(duration, 3 * distance / speed);
  // With x control points at 1/3 and 2/3, time is linear in the Bezier
  // parameter. Its initial speed is 3*y1*distance/duration and final speed 0.
  const y1 = Math.min(1, speed * duration / (3 * distance));
  return { duration, easing: `cubic-bezier(.333333,${y1},.666667,1)` };
}

interface Drag {
  id: number;
  x: number;
  y: number;
  base: number;
  offset: number;
  width: number;
  wasOpen: boolean;
  claimed: boolean;
  movingAtStart: boolean;
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
    const fade = sidebar.querySelector<HTMLElement>(":scope > .s-fade");
    if (!page || !scrim || !fade) return;
    const layers = [page, sidebar, scrim, fade];
    let viewportWidth = window.innerWidth;
    let drag: Drag | null = null;
    let timer = 0;
    let expectedOpen: boolean | null = null;
    let settleOffset = 0;
    let suppressClickUntil = 0;
    const write = (offset: number, width: number) => {
      const progress = offset / width;
      // Non-inherited properties invalidate these layers only. Shell-level
      // custom properties used to invalidate the conversation on every frame.
      page.style.transform = `translate3d(${offset}px,0,0)`;
      // Match the fully-open CSS endpoint; a held/reversed gesture must retain
      // its partial blur instead of toggling the final filter at first touch.
      page.style.filter = `blur(${.7 * progress}px)`;
      // Cutting the full rounded clip in on the first claimed move made that
      // frame visibly jump. Grow the page and its veil together with the drag.
      const radius = `calc(${progress} * clamp(36px, 12vw, 48px))`;
      page.style.borderRadius = radius;
      scrim.style.borderRadius = radius;
      sidebar.style.transform = `translate3d(${(offset - width) * .22}px,0,0)`;
      scrim.style.transform = `translate3d(${offset}px,0,0)`;
      scrim.style.opacity = `${progress}`;
      fade.style.opacity = `${1 - progress}`;
    };
    const clearMotion = (preserveTouch = false) => {
      clearTimeout(timer);
      timer = 0;
      if (!preserveTouch) drag = null;
      expectedOpen = null;
      delete shell.dataset.sidebarMotion;
      page.style.removeProperty("transform");
      page.style.removeProperty("filter");
      page.style.removeProperty("border-radius");
      sidebar.style.removeProperty("transform");
      scrim.style.removeProperty("transform");
      scrim.style.removeProperty("opacity");
      scrim.style.removeProperty("border-radius");
      fade.style.removeProperty("opacity");
      for (const layer of layers) {
        layer.style.removeProperty("transition-duration");
        layer.style.removeProperty("transition-timing-function");
      }
    };
    const syncFocus = () => {
      const active = document.activeElement;
      if (media.matches && openRef.current && active instanceof HTMLElement
          && page.contains(active)) active.blur();
      // The controller lives with the lazy sidebar; only it owns pane inertness.
      page.inert = media.matches && openRef.current;
    };
    const finishMotion = () => {
      clearTimeout(timer);
      timer = 0;
      if (drag?.claimed || shell.dataset.sidebarMotion !== "settling") return;
      // The compositor can finish before React commits the conversation. Keep
      // the inline endpoint until the class agrees, otherwise cleanup exposes
      // the previous position for a frame (or longer under streaming load).
      if (openRef.current !== (settleOffset > 0)) return;
      const transform = getComputedStyle(page).transform;
      const offset = transform === "none" ? 0 : new DOMMatrixReadOnly(transform).m41;
      if (Math.abs(offset - settleOffset) < .1) clearMotion(true);
      else timer = window.setTimeout(finishMotion, 80);
    };
    const finishLater = (duration = 260) => {
      // The timer is a fallback check, not permission to snap an unfinished
      // transition to its endpoint. A delayed state commit also calls finish.
      clearTimeout(timer);
      timer = window.setTimeout(finishMotion,
        window.matchMedia("(prefers-reduced-motion: reduce)").matches ? 0 : duration + 80);
    };
    const settle = (next: boolean, gesture: Drag, velocity = 0) => {
      clearTimeout(timer);
      // Flush the final finger position before enabling the settle transition.
      write(gesture.offset, gesture.width);
      void getComputedStyle(page).transform;
      drag = null;
      expectedOpen = next;
      settleOffset = next ? gesture.width : 0;
      const motion = sidebarReleaseMotion(gesture.offset, settleOffset, velocity);
      // Give every layer the same motion. These direct, non-inherited values
      // avoid invalidating the conversation and keep playback on CSS's
      // animation path, with no per-frame JavaScript after release.
      for (const layer of layers) {
        layer.style.transitionDuration = `${motion.duration}ms`;
        layer.style.transitionTimingFunction = motion.easing;
      }
      shell.dataset.sidebarMotion = "settling";
      write(settleOffset, gesture.width);
      finishLater(motion.duration);
      onOpenChange(next);
    };
    syncRef.current = () => {
      if (expectedOpen === openRef.current) {
        expectedOpen = null;
        // The active transition/fallback owns completion. Reading its computed
        // transform during this React commit forces an extra style flush on
        // the very frame that starts the animation. Only retry immediately if
        // completion already ran and was waiting for this delayed commit.
        if (!timer) finishMotion();
      }
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
      drag = null;
    };
    const cancel = () => {
      if (drag?.claimed) settle(drag.wasOpen, drag);
      else abandon();
    };
    const locked = (target: Element) => {
      if (target.closest(LOCKED_TARGET) || window.getSelection()?.isCollapsed === false) return true;
      // Preserve actual horizontal scrollers, including code, account filters
      // and tables. Wrapped mobile code is an ordinary navigation surface.
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
      // A touch is only a candidate until horizontal intent is known. Pausing
      // the ancestor animation here, then restarting it on vertical takeover,
      // changes the list's layers during native scrolling. Leave both the
      // animation and its cleanup alone; completion preserves this candidate.
      drag = { id: touch.identifier, x: touch.clientX, y: touch.clientY,
        base, offset: base, width, wasOpen: expectedOpen ?? openRef.current, claimed: false,
        movingAtStart: shell.dataset.sidebarMotion === "settling",
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
        // CSS reserves horizontal panning before the first input event. Chrome
        // can make later touchmoves non-cancelable after a pending first sample
        // even when it did NOT start scrolling. pointercancel, not cancelable,
        // tells us when native scrolling/zoom actually took ownership.
        if (window.getSelection()?.isCollapsed === false) { abandon(); return; }
        if (drag.movingAtStart) {
          // The old transition may have advanced (or finished) while this
          // touch was pending. A real horizontal reversal starts from today's
          // visible position, never the stale position at touchstart.
          drag.base = Math.max(0, Math.min(drag.width,
            page.getBoundingClientRect().left - shell.getBoundingClientRect().left));
        }
        clearTimeout(timer);
        timer = 0;
        shell.dataset.sidebarMotion = "dragging";
        drag.claimed = true;
      }
      if (event.cancelable) event.preventDefault();
      drag.offset = Math.max(0, Math.min(drag.width, drag.base + dx));
      drag.samples = drag.samples.filter(sample => event.timeStamp - sample.time <= 100);
      drag.samples.push({ x: touch.clientX, time: event.timeStamp });
      // Touchmove may already arrive just before paint. Deferring it through
      // another rAF leaves an older position on screen, especially on reversal.
      // These style-only writes need no layout read or React render per move.
      write(drag.offset, drag.width);
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
      settle(next, gesture, velocity);
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
    const pointerCancel = (event: PointerEvent) => {
      if (event.pointerType === "touch") cancel();
    };
    const transitionEnd = (event: TransitionEvent) => {
      if (event.target === page && event.propertyName === "transform"
          && !drag?.claimed && shell.dataset.sidebarMotion === "settling") {
        // An earlier transition's queued event must not finish its replacement.
        finishMotion();
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
    shell.addEventListener("pointercancel", pointerCancel, true);
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
      shell.removeEventListener("pointercancel", pointerCancel, true);
      shell.removeEventListener("transitionend", transitionEnd);
      window.removeEventListener("resize", resize);
      media.removeEventListener("change", resize);
    };
  }, [sidebar, onOpenChange]);
  return ref;
}
