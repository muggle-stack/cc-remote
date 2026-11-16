import type { Terminal } from "@xterm/xterm";
import { terminalSelectedText, terminalPoint, selectTerminalWord } from "./dsh-terminal-selection";

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

export type TerminalDirection = "ArrowUp" | "ArrowDown" | "ArrowLeft" | "ArrowRight";
export interface TerminalTouchFeedback { direction: TerminalDirection | null; speed: number }

interface TerminalTouchActions {
  ready(): boolean;
  send(key: string): void;
  activateTouch(): void;
  openKeyboard(): void;
  select(): void;
  clearSelection(): void;
  feedback(value: TerminalTouchFeedback | null): void;
  resize(): void;
}

// One gesture owns the contact until release. In particular, a slow scroll
// cannot turn into an arrow hold after its long-press deadline has elapsed.
export function attachTerminalTouch(
  host: HTMLElement, terminal: Terminal, actions: TerminalTouchActions,
) {
  type Contact = { x: number; y: number };
  const contacts = new Map<number, Contact>();
  let gesture: (Contact & {
    lastY: number; remainder: number; mode: "pending" | "scroll" | "hold" | "arrows" | "dismiss" | "pinch";
  }) | undefined;
  let hold: ReturnType<typeof setTimeout> | undefined;
  let repeat: ReturnType<typeof setTimeout> | undefined;
  let direction: TerminalDirection | null = null;
  let interval = 180;
  let lastTap: (Contact & { time: number }) | undefined;
  let lastTouch = -Infinity;
  let reportingMouse = false;
  let pinch: { distance: number; size: number } | undefined;

  const clear = () => {
    clearTimeout(hold); clearTimeout(repeat);
    gesture = undefined; direction = null; pinch = undefined;
    actions.feedback(null);
  };
  const cancel = () => { clear(); contacts.clear(); lastTap = undefined; };
  const repeatKey = () => {
    if (!direction || !actions.ready()) { clear(); return; }
    actions.send(direction);
    repeat = setTimeout(repeatKey, interval);
  };
  const distance = () => {
    const [a, b] = [...contacts.values()];
    return Math.hypot(a.x - b.x, a.y - b.y);
  };
  const start = (event: PointerEvent) => {
    if (event.pointerType !== "touch") return;
    lastTouch = performance.now();
    event.preventDefault(); event.stopPropagation();
    actions.activateTouch();
    if (event.isTrusted) host.setPointerCapture(event.pointerId);
    contacts.set(event.pointerId, { x: event.clientX, y: event.clientY });
    if (contacts.size > 1) {
      clear(); lastTap = undefined;
      gesture = { x: 0, y: 0, lastY: 0, remainder: 0, mode: "pinch" };
      if (contacts.size === 2) pinch = { distance: distance(), size: terminal.options.fontSize ?? 13 };
      return;
    }
    clear();
    gesture = { x: event.clientX, y: event.clientY, lastY: event.clientY, remainder: 0,
      mode: terminalSelectedText(terminal) ? "dismiss" : "pending" };
    if (gesture.mode === "dismiss") return;
    hold = setTimeout(() => {
      if (gesture?.mode !== "pending") return;
      gesture.mode = "hold"; lastTap = undefined;
      actions.feedback({ direction: null, speed: 0 });
    }, 450);
  };
  const move = (event: PointerEvent) => {
    if (event.pointerType !== "touch" || !contacts.has(event.pointerId) || !gesture) return;
    event.preventDefault(); event.stopPropagation();
    contacts.set(event.pointerId, { x: event.clientX, y: event.clientY });
    if (gesture.mode === "pinch") {
      if (contacts.size !== 2 || !pinch?.distance) return;
      const size = Math.max(9, Math.min(24, Math.round(pinch.size * distance() / pinch.distance)));
      if (size !== terminal.options.fontSize) { terminal.options.fontSize = size; actions.resize(); }
      return;
    }
    const dx = event.clientX - gesture.x, dy = event.clientY - gesture.y;
    const displacement = Math.hypot(dx, dy);
    if (gesture.mode === "pending") {
      if (displacement < 8) return;
      clearTimeout(hold); lastTap = undefined;
      gesture.mode = "scroll";
    }
    if (gesture.mode === "hold" || gesture.mode === "arrows") {
      if (!actions.ready()) return;
      const next = displacement < 14 ? null : Math.abs(dx) > Math.abs(dy)
        ? (dx > 0 ? "ArrowRight" : "ArrowLeft") : (dy > 0 ? "ArrowDown" : "ArrowUp");
      const speed = displacement < 48 ? 1 : displacement < 96 ? 2 : 3;
      interval = [0, 180, 100, 50][speed];
      if (next !== direction) {
        clearTimeout(repeat); direction = next;
        if (next) { gesture.mode = "arrows"; repeatKey(); }
      }
      actions.feedback({ direction: next, speed: next ? speed : 0 });
      return;
    }
    if (gesture.mode !== "scroll") return;
    const height = host.querySelector(".xterm-screen")?.getBoundingClientRect().height ?? 0;
    if (!height || !terminal.rows) return;
    gesture.remainder += (gesture.lastY - event.clientY) / (height / terminal.rows);
    gesture.lastY = event.clientY;
    const lines = Math.trunc(gesture.remainder);
    if (!lines) return;
    gesture.remainder -= lines;
    if (terminal.buffer.active.type === "normal" && terminal.modes.mouseTrackingMode === "none") {
      terminal.scrollLines(lines);
    } else {
      // Let xterm translate wheel input for alternate-screen and mouse-reporting
      // applications (vim, less, tmux) using their current terminal modes.
      host.querySelector(".xterm")?.dispatchEvent(new WheelEvent("wheel", {
        bubbles: true, cancelable: true, clientX: event.clientX, clientY: event.clientY,
        deltaMode: WheelEvent.DOM_DELTA_LINE, deltaY: lines,
      }));
    }
  };
  const end = (event: PointerEvent) => {
    if (event.pointerType !== "touch" || !contacts.has(event.pointerId)) return;
    event.preventDefault(); event.stopPropagation();
    contacts.delete(event.pointerId);
    const finished = gesture;
    if (contacts.size) { clear(); return; }
    clear();
    if (finished?.mode === "hold") {
      const point = terminalPoint(host, terminal, finished.x, finished.y);
      if (point) { selectTerminalWord(terminal, point); actions.select(); }
    } else if (finished?.mode === "dismiss") {
      actions.clearSelection();
    } else if (finished?.mode === "pending") {
      const now = performance.now();
      if (lastTap && now - lastTap.time < 320 && Math.hypot(finished.x - lastTap.x, finished.y - lastTap.y) < 24) {
        if (actions.ready()) actions.send("Tab");
        lastTap = undefined;
      } else {
        lastTap = { x: finished.x, y: finished.y, time: now };
        actions.openKeyboard();
        if (terminal.modes.mouseTrackingMode !== "none" && actions.ready()) {
          reportingMouse = true;
          for (const type of ["mousedown", "mouseup"]) host.querySelector(".xterm")?.dispatchEvent(new MouseEvent(type, {
            bubbles: true, cancelable: true, button: 0, clientX: finished.x, clientY: finished.y,
          }));
          reportingMouse = false;
        }
      }
    }
  };
  const lostCapture = (event: PointerEvent) => {
    if (contacts.has(event.pointerId)) cancel();
  };
  const suppressMouse = (event: MouseEvent) => {
    if (!reportingMouse && performance.now() - lastTouch < 1000) { event.preventDefault(); event.stopPropagation(); }
  };
  const context = (event: MouseEvent) => {
    if (contacts.size || performance.now() - lastTouch < 1000) { event.preventDefault(); event.stopPropagation(); }
  };
  host.addEventListener("pointerdown", start, { capture: true });
  host.addEventListener("pointermove", move, { capture: true });
  host.addEventListener("pointerup", end, { capture: true });
  host.addEventListener("pointercancel", cancel);
  host.addEventListener("lostpointercapture", lostCapture);
  host.addEventListener("mousedown", suppressMouse, { capture: true });
  host.addEventListener("click", suppressMouse, { capture: true });
  host.addEventListener("contextmenu", context, { capture: true });
  window.addEventListener("blur", cancel);
  document.addEventListener("visibilitychange", cancel);
  return { cancel, dispose: () => {
    cancel();
    host.removeEventListener("pointerdown", start, { capture: true });
    host.removeEventListener("pointermove", move, { capture: true });
    host.removeEventListener("pointerup", end, { capture: true });
    host.removeEventListener("pointercancel", cancel);
    host.removeEventListener("lostpointercapture", lostCapture);
    host.removeEventListener("mousedown", suppressMouse, { capture: true });
    host.removeEventListener("click", suppressMouse, { capture: true });
    host.removeEventListener("contextmenu", context, { capture: true });
    window.removeEventListener("blur", cancel);
    document.removeEventListener("visibilitychange", cancel);
  } };
}
