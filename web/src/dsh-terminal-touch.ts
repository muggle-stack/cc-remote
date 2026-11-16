import type { Terminal } from "@xterm/xterm";

// Accumulate trackpad pixels before moving whole rows. xterm's viewport rounds
// each tiny wheel event back to the same row, losing slow two-finger gestures.
export function attachTerminalWheelScroll(host: HTMLElement, terminal: Terminal): () => void {
  let remainder = 0;
  const wheel = (event: WheelEvent) => {
    const buffer = terminal.buffer.active;
    if (event.defaultPrevented || event.ctrlKey || event.metaKey || event.shiftKey
      || buffer.type !== "normal" || !buffer.baseY
      || terminal.modes.mouseTrackingMode !== "none"
      || Math.abs(event.deltaY) <= Math.abs(event.deltaX)) {
      remainder = 0;
      return;
    }
    const height = host.querySelector(".xterm-screen")?.getBoundingClientRect().height ?? 0;
    if (!height || !terminal.rows) return;
    const rowHeight = height / terminal.rows;
    let delta = event.deltaMode === WheelEvent.DOM_DELTA_LINE ? event.deltaY
      : event.deltaMode === WheelEvent.DOM_DELTA_PAGE ? event.deltaY * terminal.rows
        : event.deltaY / rowHeight;
    delta *= terminal.options.scrollSensitivity ?? 1;
    if (event.altKey) delta *= terminal.options.fastScrollSensitivity ?? 5;
    if (remainder * delta < 0) remainder = 0;
    remainder += delta;
    const lines = Math.trunc(remainder);
    if (lines) {
      terminal.scrollLines(lines);
      remainder -= lines;
    }
    event.preventDefault();
    event.stopPropagation();
  };
  host.addEventListener("wheel", wheel, { passive: false, capture: true });
  return () => host.removeEventListener("wheel", wheel, { capture: true });
}

interface TerminalGestureInput {
  ready(): boolean;
  send(action: "history" | "complete"): void;
}

// Ordinary drags browse scrollback. At an editable prompt already at the
// bottom, an upward swipe recalls history and hold-right accepts a suggestion.
export function attachTerminalTouchScroll(
  host: HTMLElement, terminal: Terminal, input?: TerminalGestureInput,
): () => void {
  let gesture: {
    id: number; x: number; y: number; lastY: number; remainder: number;
    startedAt: number; atPrompt: boolean; action?: "scroll" | "history" | "complete";
    distance: number;
  } | undefined;
  const clear = () => { gesture = undefined; };
  const ready = () => terminal.buffer.active.type === "normal"
    && terminal.buffer.active.viewportY === terminal.buffer.active.baseY
    && terminal.modes.mouseTrackingMode === "none"
    && !terminal.hasSelection() && input?.ready() === true;
  const start = (event: TouchEvent) => {
    clear();
    if (event.touches.length !== 1 || terminal.buffer.active.type !== "normal"
      || terminal.hasSelection()
      || (event.target instanceof Element && event.target.closest(".scrollbar"))) return;
    const touch = event.touches[0];
    gesture = { id: touch.identifier, x: touch.clientX, y: touch.clientY,
      lastY: touch.clientY, remainder: 0, startedAt: performance.now(),
      atPrompt: ready(), distance: 0 };
  };
  const move = (event: TouchEvent) => {
    if (!gesture) return;
    if (event.touches.length !== 1 || terminal.buffer.active.type !== "normal"
      || terminal.hasSelection()) { clear(); return; }
    const touch = event.touches[0];
    if (touch.identifier !== gesture.id) { clear(); return; }
    const dx = touch.clientX - gesture.x, dy = touch.clientY - gesture.y;
    if (!gesture.action) {
      if (Math.max(Math.abs(dx), Math.abs(dy)) < 8) return;
      if (gesture.atPrompt && ready() && dy < 0 && -dy > Math.abs(dx) * 1.5) {
        gesture.action = "history";
      } else if (gesture.atPrompt && ready() && dx > Math.abs(dy) * 1.5
        && performance.now() - gesture.startedAt >= 250) {
        gesture.action = "complete";
      } else if (Math.abs(dy) > Math.abs(dx) && terminal.buffer.active.baseY) {
        gesture.action = "scroll";
      } else { clear(); return; }
    }
    if (event.cancelable) event.preventDefault();
    if (gesture.action !== "scroll") {
      gesture.distance = gesture.action === "history"
        ? -dy - Math.abs(dx) : dx - Math.abs(dy);
      return;
    }
    const height = host.querySelector(".xterm-screen")?.getBoundingClientRect().height ?? 0;
    if (!height || !terminal.rows) { clear(); return; }
    gesture.remainder += (gesture.lastY - touch.clientY) / (height / terminal.rows);
    gesture.lastY = touch.clientY;
    const lines = Math.trunc(gesture.remainder);
    if (lines) { terminal.scrollLines(lines); gesture.remainder -= lines; }
  };
  const end = (event: TouchEvent) => {
    const finished = gesture;
    clear();
    if (event.touches.length || !finished || finished.distance < 40 || !ready()
      || finished.action !== "history" && finished.action !== "complete") return;
    if (event.cancelable) event.preventDefault();
    input?.send(finished.action);
  };
  host.addEventListener("touchstart", start, { passive: true });
  host.addEventListener("touchmove", move, { passive: false });
  host.addEventListener("touchend", end, { passive: false });
  host.addEventListener("touchcancel", clear);
  host.addEventListener("contextmenu", clear);
  return () => {
    clear();
    host.removeEventListener("touchstart", start);
    host.removeEventListener("touchmove", move);
    host.removeEventListener("touchend", end);
    host.removeEventListener("touchcancel", clear);
    host.removeEventListener("contextmenu", clear);
  };
}
