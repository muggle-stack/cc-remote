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

// xterm 6's virtual viewport does not implement touch scrolling.
// Move its buffer through the public API; a swipe must never become
// arrow keys or other PTY input, including in full-screen terminal programs.
export function attachTerminalTouchScroll(host: HTMLElement, terminal: Terminal): () => void {
  let gesture: { id: number; x: number; y: number; lastY: number; remainder: number; claimed: boolean } | undefined;
  const clear = () => { gesture = undefined; };
  const start = (event: TouchEvent) => {
    clear();
    if (event.touches.length !== 1 || terminal.buffer.active.type !== "normal"
      || !terminal.buffer.active.baseY || terminal.hasSelection()
      || (event.target instanceof Element && event.target.closest(".scrollbar"))) return;
    const touch = event.touches[0];
    gesture = { id: touch.identifier, x: touch.clientX, y: touch.clientY,
      lastY: touch.clientY, remainder: 0, claimed: false };
  };
  const move = (event: TouchEvent) => {
    if (!gesture) return;
    if (event.touches.length !== 1 || terminal.buffer.active.type !== "normal") { clear(); return; }
    const touch = event.touches[0];
    if (touch.identifier !== gesture.id) { clear(); return; }
    if (!gesture.claimed) {
      const dx = Math.abs(touch.clientX - gesture.x), dy = Math.abs(touch.clientY - gesture.y);
      if (Math.max(dx, dy) < 6) return;
      if (dx > dy) { clear(); return; }
      gesture.claimed = true;
    }
    if (event.cancelable) event.preventDefault();
    const height = host.querySelector(".xterm-screen")?.getBoundingClientRect().height ?? 0;
    if (!height || !terminal.rows) { clear(); return; }
    gesture.remainder += (gesture.lastY - touch.clientY) / (height / terminal.rows);
    gesture.lastY = touch.clientY;
    const lines = Math.trunc(gesture.remainder);
    if (lines) { terminal.scrollLines(lines); gesture.remainder -= lines; }
  };
  host.addEventListener("touchstart", start, { passive: true });
  host.addEventListener("touchmove", move, { passive: false });
  host.addEventListener("touchend", clear);
  host.addEventListener("touchcancel", clear);
  return () => {
    clear();
    host.removeEventListener("touchstart", start);
    host.removeEventListener("touchmove", move);
    host.removeEventListener("touchend", clear);
    host.removeEventListener("touchcancel", clear);
  };
}
