import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import {
  createMobileViewportSync,
  type MobileViewportBindings,
  type MobileViewportEvent,
  type ViewportReading,
} from "../src/use-mobile-viewport.ts";

const attributes = new Map<string, string>();
const css = new Map<string, string>();
const listeners = new Map<MobileViewportEvent, Set<() => void>>();
const frames = new Map<number, () => void>();
const delays = new Map<number, () => void>();
let nextId = 1;
let focused = false;
let reading: ViewportReading = {
  height: 844,
  layoutHeight: 844,
  offsetTop: 0,
  scale: 1,
};

const bindings: MobileViewportBindings = {
  readViewport: () => reading,
  setCssProperty: (name, value) => { css.set(name, value); },
  clearCssProperty: (name) => { css.delete(name); },
  setRootAttribute: (name, value) => { attributes.set(name, value); },
  clearRootAttribute: (name) => { attributes.delete(name); },
  listen: (event, listener) => {
    const subscribers = listeners.get(event) ?? new Set<() => void>();
    subscribers.add(listener);
    listeners.set(event, subscribers);
    return () => subscribers.delete(listener);
  },
  requestFrame: (listener) => {
    const id = nextId++;
    frames.set(id, listener);
    return id;
  },
  cancelFrame: (id) => { frames.delete(id); },
  setDelay: (listener) => {
    const id = nextId++;
    delays.set(id, listener);
    return id;
  },
  clearDelay: (id) => { delays.delete(id); },
  isEditableFocused: () => focused,
  resetLayoutScroll: () => {},
};

function emit(event: MobileViewportEvent): void {
  for (const listener of listeners.get(event) ?? []) listener();
  const pending = [...frames.values()];
  frames.clear();
  for (const frame of pending) frame();
}

const stop = createMobileViewportSync(bindings);
assert.equal(attributes.get("data-short-viewport"), "false");

focused = true;
reading = { height: 510, layoutHeight: 844, offsetTop: 0, scale: 1 };
emit("viewport-resize");
assert.equal(attributes.get("data-short-viewport"), "ime",
  "a focused editor plus a keyboard-sized inset must hide auxiliary controls");
assert.equal(css.get("--content-safe-bottom"), "0px",
  "an open keyboard already covers the bottom safe area");

reading = { height: 510, layoutHeight: 844, offsetTop: 24, documentScrollTop: 60, scale: 1 };
emit("document-scroll");
assert.equal(css.get("--app-document-top"), "84px",
  "the document-positioned shell must include Safari's focus scroll");
assert.equal(css.get("--app-offset-top"), "24px",
  "fixed overlays must keep their layout-viewport coordinates");
assert.equal(css.get("--keyboard-inset"), "310px",
  "document scrolling must not enlarge the keyboard inset");
reading = { ...reading, documentScrollTop: 90 };
emit("document-scroll");
assert.equal(css.get("--app-document-top"), "114px",
  "a document-only scroll must update the shell even without viewport resize");

focused = false;
emit("viewport-resize");
assert.equal(attributes.get("data-short-viewport"), "true",
  "a short visual viewport without an editor must not masquerade as a keyboard");

focused = true;
reading = { height: 420, layoutHeight: 844, offsetTop: 30, scale: 1.5 };
emit("viewport-resize");
assert.equal(attributes.get("data-short-viewport"), "false",
  "pinch zoom must not hide the Goal monitor");
assert.equal(css.get("--content-safe-bottom"), "env(safe-area-inset-bottom, 0px)");
assert.equal(css.get("--app-document-top"), "0px",
  "pinch zoom remains under the user's control");

const goalCss = readFileSync(resolve(process.cwd(), "src/App.css"), "utf8");
assert.match(goalCss,
  /:root\[data-short-viewport="ime"\] \.goal-chip-wrap,[\s\S]{0,100}\.goal-suspense \{ display:none; \}/,
  "the keyboard state must remove the compact Goal row from composer layout");

stop();
assert.equal(attributes.has("data-short-viewport"), false);
assert.equal(frames.size, 0);
assert.equal(delays.size, 0);
assert.equal(css.has("--content-safe-bottom"), false);
assert.equal(css.has("--app-document-top"), false);
