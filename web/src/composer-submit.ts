import type { Engine } from "./protocol";
import type { State } from "./protocol";

export type SendMode = "steer" | "queue";

/** Only a plain Tab with a draft in a running task can enqueue. Callers give
 * completion menus and IME composition priority over this shortcut. */
export function shouldQueueOnTab(
  event: {
    key: string;
    shiftKey: boolean;
    ctrlKey: boolean;
    altKey: boolean;
    metaKey: boolean;
    repeat: boolean;
  },
  state: State,
  hasPayload: boolean,
): boolean {
  return event.key === "Tab" && !event.shiftKey && !event.ctrlKey
    && !event.altKey && !event.metaKey && !event.repeat
    && state === "running" && hasPayload;
}

export type BusySubmitAction =
  | "steer"
  | "replace"
  | "enqueue"
  | "noop";

/** Decide how a submit should behave while the runtime is not idle.
 *
 * A payload-less submit is always a no-op.  Stopping a running turn is an
 * explicit button action and must never be inferred from an empty Enter press.
 * Once the wrapper reports interrupting/draining, a payload may update the
 * pending message, but must not enqueue a second interrupt command.
 */
export function classifyBusySubmit(
  state: State,
  mode: SendMode,
  _engine: Engine,
  hasPayload: boolean,
): BusySubmitAction {
  if (state === "idle") return "noop";
  if (!hasPayload) return "noop";
  if (mode === "queue") return "enqueue";
  if (state !== "running") return "replace";
  return "steer";
}

export function isComposerBusy(state: State): boolean {
  return state !== "idle";
}

export function isInterruptSettling(state: State): boolean {
  return state === "interrupting" || state === "draining";
}

export function isSettlingStopDisabled(
  state: State, hasPayload: boolean,
): boolean {
  return isInterruptSettling(state) && !hasPayload;
}
