import type { Terminal } from "@xterm/xterm";

export function terminalSelectedText(terminal: Terminal): string {
  const selection = window.getSelection();
  if (selection && !selection.isCollapsed && selection.anchorNode && selection.focusNode
    && terminal.element?.contains(selection.anchorNode) && terminal.element.contains(selection.focusNode)) {
    return selection.toString();
  }
  return terminal.getSelection();
}

export function terminalTextAtPoint(host: HTMLElement, terminal: Terminal, x: number, y: number): boolean {
  const screen = host.querySelector(".xterm-screen")?.getBoundingClientRect();
  if (!screen?.width || !screen.height) return false;
  const column = Math.floor((x - screen.left) / (screen.width / terminal.cols));
  const row = Math.floor((y - screen.top) / (screen.height / terminal.rows));
  const line = row >= 0 && row < terminal.rows ? terminal.buffer.active.getLine(terminal.buffer.active.viewportY + row) : undefined;
  const cell = line?.getCell(column);
  return !!(cell?.getChars().trim() || cell?.getWidth() === 0 && line?.getCell(column - 1)?.getChars().trim());
}

export function attachTerminalNativeSelection(host: HTMLElement, terminal: Terminal): () => void {
  let touchedAt = -Infinity;
  let hold: ReturnType<typeof setTimeout> | undefined;
  let start: { x: number; y: number } | undefined;
  let heldText = false;
  const className = "dsh-terminal-touch-selection";
  host.classList.toggle(className, window.matchMedia("(any-pointer: coarse)").matches);
  const clearHold = () => { clearTimeout(hold); start = undefined; };
  const touch = (event: TouchEvent) => {
    clearHold();
    heldText = false;
    touchedAt = performance.now(); host.classList.add(className);
    terminal.clearSelection();
    if (event.touches.length !== 1) return;
    const { clientX: x, clientY: y } = event.touches[0];
    if (terminalTextAtPoint(host, terminal, x, y)) {
      start = { x, y };
      // Blurring before native long-press selection prevents the focused
      // hidden input from replacing that selection with its own caret.
      hold = setTimeout(() => { heldText = true; terminal.blur(); }, 250);
    }
  };
  const move = (event: TouchEvent) => {
    if (start && (event.touches.length !== 1
      || Math.abs(event.touches[0].clientX - start.x) >= 8
      || Math.abs(event.touches[0].clientY - start.y) >= 8)) clearHold();
  };
  const mouseDown = (event: MouseEvent) => {
    if (!host.classList.contains(className)) return;
    if (performance.now() - touchedAt > 1200 && !terminalSelectedText(terminal)) {
      host.classList.remove(className);
      return;
    }
    // xterm's synthetic mouse selection cancels the browser's native handles.
    // Keep the default touch selection action and skip only that mouse handler.
    if (heldText || terminalSelectedText(terminal)) event.stopPropagation();
  };
  const contextMenu = (event: MouseEvent) => {
    if (host.classList.contains(className)
      && (heldText && performance.now() - touchedAt < 1200 || terminalSelectedText(terminal))) event.stopPropagation();
  };
  const click = (event: MouseEvent) => {
    if (!event.defaultPrevented && performance.now() - touchedAt < 1200
      && !terminalSelectedText(terminal)) terminal.focus();
  };
  host.addEventListener("touchstart", touch, { passive: true, capture: true });
  host.addEventListener("touchmove", move, { passive: true, capture: true });
  host.addEventListener("touchend", clearHold, { capture: true });
  host.addEventListener("touchcancel", clearHold, { capture: true });
  host.addEventListener("mousedown", mouseDown, { capture: true });
  host.addEventListener("contextmenu", contextMenu, { capture: true });
  host.addEventListener("click", click);
  return () => {
    clearHold();
    host.removeEventListener("touchstart", touch, { capture: true });
    host.removeEventListener("touchmove", move, { capture: true });
    host.removeEventListener("touchend", clearHold, { capture: true });
    host.removeEventListener("touchcancel", clearHold, { capture: true });
    host.removeEventListener("mousedown", mouseDown, { capture: true });
    host.removeEventListener("contextmenu", contextMenu, { capture: true });
    host.removeEventListener("click", click);
    host.classList.remove(className);
  };
}
