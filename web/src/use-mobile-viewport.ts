import { useEffect } from "react";

type ViewportListener = () => void;

export type MobileViewportEvent =
  | "viewport-resize"
  | "viewport-scroll"
  | "window-resize"
  | "orientation-change"
  | "page-show"
  | "focus-in"
  | "focus-out";

export interface ViewportReading {
  height: number;
  layoutHeight: number;
  offsetTop: number;
  scale: number;
}

/** Browser operations are injected so the iOS settling behavior is unit-testable. */
export interface MobileViewportBindings {
  readViewport(): ViewportReading;
  setCssProperty(name: string, value: string): void;
  clearCssProperty(name: string): void;
  listen(event: MobileViewportEvent, listener: ViewportListener): () => void;
  requestFrame(listener: ViewportListener): number;
  cancelFrame(id: number): void;
  setDelay(listener: ViewportListener, delayMs: number): number;
  clearDelay(id: number): void;
  isEditableFocused(): boolean;
  resetLayoutScroll(): void;
}

const APP_HEIGHT = "--app-height";
const APP_OFFSET_TOP = "--app-offset-top";
const KEYBOARD_INSET = "--keyboard-inset";
const SETTLE_DELAYS_MS = [80, 260] as const;

function finitePositive(value: number, fallback: number): number {
  return Number.isFinite(value) && value > 0 ? value : fallback;
}

function px(value: number): string {
  return `${Math.round(value * 100) / 100}px`;
}

/**
 * Keep the app shell aligned with the visual viewport while a mobile keyboard
 * animates. Focus-out/orientation events get two delayed reads because iOS can
 * report the keyboard-sized viewport for a short time after the first resize.
 */
export function createMobileViewportSync(bindings: MobileViewportBindings): () => void {
  let stopped = false;
  let frameId: number | null = null;
  const delayIds = new Set<number>();

  const clearSettleDelays = () => {
    for (const id of delayIds) bindings.clearDelay(id);
    delayIds.clear();
  };

  const apply = () => {
    if (stopped) return;
    const reading = bindings.readViewport();
    const layoutHeight = finitePositive(reading.layoutHeight, reading.height);
    const height = finitePositive(reading.height, layoutHeight);
    const offsetTop = Number.isFinite(reading.offsetTop)
      ? Math.max(0, reading.offsetTop)
      : 0;
    const scale = finitePositive(reading.scale, 1);

    // A pinched page also has a shorter visual viewport; do not mistake that
    // user-controlled zoom for an on-screen keyboard.
    const keyboardInset = scale <= 1.01
      ? Math.max(0, layoutHeight - height - offsetTop)
      : 0;
    bindings.setCssProperty(APP_HEIGHT, px(height));
    bindings.setCssProperty(APP_OFFSET_TOP, px(scale <= 1.01 ? offsetTop : 0));
    bindings.setCssProperty(KEYBOARD_INSET, px(keyboardInset));
  };

  const schedule = () => {
    if (stopped) return;
    if (frameId !== null) bindings.cancelFrame(frameId);
    frameId = bindings.requestFrame(() => {
      frameId = null;
      apply();
    });
  };

  const settle = () => {
    clearSettleDelays();
    schedule();
    for (const delay of SETTLE_DELAYS_MS) {
      const id = bindings.setDelay(() => {
        delayIds.delete(id);
        if (stopped) return;
        const reading = bindings.readViewport();
        if (!bindings.isEditableFocused() && reading.scale <= 1.01) {
          // The document itself is not meant to scroll; chat panes own their
          // scroll positions. This clears Safari's residual focus pan without
          // disturbing deliberate pinch zoom.
          bindings.resetLayoutScroll();
        }
        schedule();
      }, delay);
      delayIds.add(id);
    }
  };

  const unsubscribers = [
    bindings.listen("viewport-resize", schedule),
    bindings.listen("viewport-scroll", schedule),
    bindings.listen("window-resize", schedule),
    bindings.listen("orientation-change", settle),
    bindings.listen("page-show", settle),
    bindings.listen("focus-in", schedule),
    bindings.listen("focus-out", settle),
  ];

  apply();

  return () => {
    stopped = true;
    for (const unsubscribe of unsubscribers) unsubscribe();
    if (frameId !== null) bindings.cancelFrame(frameId);
    clearSettleDelays();
    bindings.clearCssProperty(APP_HEIGHT);
    bindings.clearCssProperty(APP_OFFSET_TOP);
    bindings.clearCssProperty(KEYBOARD_INSET);
  };
}

function isEditableElement(element: Element | null): boolean {
  return !!element?.matches(
    'textarea:not([disabled]), select:not([disabled]), input:not([type="button"]):not([type="submit"]):not([type="reset"]):not([type="checkbox"]):not([type="radio"]):not([type="file"]):not([type="hidden"]):not([disabled]), [contenteditable="true"]',
  );
}

function browserBindings(): MobileViewportBindings {
  return {
    readViewport: () => {
      const viewport = window.visualViewport;
      const layoutHeight = Math.max(window.innerHeight, document.documentElement.clientHeight);
      return {
        height: viewport?.height ?? layoutHeight,
        layoutHeight,
        offsetTop: viewport?.offsetTop ?? 0,
        scale: viewport?.scale ?? 1,
      };
    },
    setCssProperty: (name, value) => document.documentElement.style.setProperty(name, value),
    clearCssProperty: (name) => document.documentElement.style.removeProperty(name),
    listen: (event, listener) => {
      let target: EventTarget = window;
      let eventName: string = event;
      if (event === "viewport-resize" || event === "viewport-scroll") {
        if (!window.visualViewport) return () => {};
        target = window.visualViewport;
        eventName = event === "viewport-resize" ? "resize" : "scroll";
      } else if (event === "window-resize") {
        eventName = "resize";
      } else if (event === "orientation-change") {
        eventName = "orientationchange";
      } else if (event === "page-show") {
        eventName = "pageshow";
      } else {
        target = document;
        eventName = event === "focus-in" ? "focusin" : "focusout";
      }
      target.addEventListener(eventName, listener, { passive: true });
      return () => target.removeEventListener(eventName, listener);
    },
    requestFrame: (listener) => window.requestAnimationFrame(listener),
    cancelFrame: (id) => window.cancelAnimationFrame(id),
    setDelay: (listener, delayMs) => window.setTimeout(listener, delayMs),
    clearDelay: (id) => window.clearTimeout(id),
    isEditableFocused: () => isEditableElement(document.activeElement),
    resetLayoutScroll: () => window.scrollTo(0, 0),
  };
}

/** Install visual-viewport synchronization for the lifetime of the React app. */
export function useMobileViewport(): void {
  useEffect(() => createMobileViewportSync(browserBindings()), []);
}
