import type { Terminal } from "@xterm/xterm";

export interface TerminalModifiers { ctrl: boolean; alt: boolean; shift: boolean }
export const noTerminalModifiers: TerminalModifiers = { ctrl: false, alt: false, shift: false };

export function terminalKey(terminal: Terminal, key: string): string {
  const arrows: Record<string, string> = { ArrowUp: "A", ArrowDown: "B", ArrowRight: "C", ArrowLeft: "D" };
  if (arrows[key]) return (terminal.modes.applicationCursorKeysMode ? "\x1bO" : "\x1b[") + arrows[key];
  const keys: Record<string, string> = { Enter: "\r", Backspace: "\x7f", Escape: "\x1b", Tab: "\t", "Shift+Tab": "\x1b[Z", "Ctrl+C": "\x03" };
  return keys[key] ?? key;
}

export function modifyTerminalInput(data: string, modifiers: TerminalModifiers): string {
  const arrow = data[0] === "\x1b" && /^[ABCDHF]$/.test(data.slice(2)) && (data[1] === "[" || data[1] === "O");
  const modifier = Number(modifiers.shift) + 2 * Number(modifiers.alt) + 4 * Number(modifiers.ctrl);
  if (arrow && modifier) return `\x1b[1;${1 + modifier}${data[2]}`;
  if (modifiers.shift && data === "\t") data = "\x1b[Z";
  else if (modifiers.shift && data.length === 1) data = data.toUpperCase();
  if (modifiers.ctrl && data.length === 1) {
    const code = data.toUpperCase().charCodeAt(0);
    if (code >= 64 && code <= 95) data = String.fromCharCode(code - 64);
    else if (data === " " || data === "2") data = "\0";
    else if (data === "?") data = "\x7f";
  }
  return modifiers.alt ? "\x1b" + data : data;
}

/** All mobile focus requests pass through the explicit keyboard preference. */
export function attachTerminalKeyboard(host: HTMLElement, terminal: Terminal, onChange: (open: boolean) => void) {
  const textarea = terminal.textarea!;
  let touch = window.matchMedia("(any-pointer: coarse)").matches;
  let open = !touch;
  const sync = () => {
    textarea.inputMode = touch && !open ? "none" : "text";
    textarea.tabIndex = touch && !open ? -1 : 0;
    host.tabIndex = touch && !open ? 0 : -1;
    host.dataset.keyboard = open ? "open" : "closed";
    onChange(open);
  };
  const close = () => {
    const ownedFocus = host.contains(document.activeElement);
    open = false;
    sync();
    terminal.blur();
    if (touch && ownedFocus) host.focus({ preventScroll: true });
  };
  const activateTouch = () => {
    if (touch) return;
    touch = true;
    close();
  };
  const focus = () => {
    if (touch && !open) terminal.blur();
  };
  const blur = () => {
    if (touch) { open = false; sync(); }
  };
  // Android's Back button can hide the IME while leaving its textarea focused.
  // Preserve that dismissal when later output or clipboard work restores focus.
  let imeVisible = document.documentElement.dataset.shortViewport === "ime";
  const viewport = new MutationObserver(() => {
    const nextVisible = document.documentElement.dataset.shortViewport === "ime";
    const dismissed = imeVisible && !nextVisible;
    imeVisible = nextVisible;
    if (touch && open && dismissed) close();
  });
  viewport.observe(document.documentElement, { attributes: true, attributeFilter: ["data-short-viewport"] });
  // Physical keyboards still work when the mobile on-screen keyboard is hidden.
  const hardwareKey = (event: KeyboardEvent) => {
    if (event.target !== host || event.isComposing || event.metaKey) return;
    if (event.key.length !== 1 && !["Enter", "Backspace", "Escape", "Tab", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"].includes(event.key)) return;
    event.preventDefault(); event.stopPropagation();
    terminal.input(modifyTerminalInput(terminalKey(terminal, event.key), {
      ctrl: event.ctrlKey, alt: event.altKey, shift: event.shiftKey,
    }), true);
  };
  textarea.addEventListener("focus", focus);
  textarea.addEventListener("blur", blur);
  host.addEventListener("keydown", hardwareKey);
  sync();
  return {
    activateTouch,
    open: () => {
      open = true; sync();
      terminal.focus();
    },
    close,
    restore: () => { if (!touch || open) terminal.focus(); },
    dispose: () => {
      textarea.removeEventListener("focus", focus);
      textarea.removeEventListener("blur", blur);
      host.removeEventListener("keydown", hardwareKey);
      viewport.disconnect();
      delete host.dataset.keyboard;
    },
  };
}
