import { useEffect, useRef } from "react";
import { ImeSubmitGuard, type SubmitKeyEvent } from "./ime-submit";

type TextControl = HTMLInputElement | HTMLTextAreaElement;

/**
 * Shared IME-safe submit plumbing for text controls outside the main composer.
 *
 * Pointer submits are deferred by one task so a blur-triggered compositionend
 * can update the DOM value first.  The callback always reads that authoritative
 * DOM value instead of a potentially stale React render closure.
 */
export function useImeSubmit<T extends TextControl>(
  submitValue: (value: string) => void,
) {
  const inputRef = useRef<T>(null);
  const guardRef = useRef(new ImeSubmitGuard());
  const timerRef = useRef<number | null>(null);
  const submitRef = useRef(submitValue);
  submitRef.current = submitValue;

  useEffect(() => () => {
    if (timerRef.current !== null) window.clearTimeout(timerRef.current);
  }, []);

  const requestSubmit = () => {
    if (timerRef.current !== null) return;
    timerRef.current = window.setTimeout(() => {
      timerRef.current = null;
      submitRef.current(inputRef.current?.value ?? "");
    }, 0);
  };

  return {
    inputRef,
    startComposition: () => guardRef.current.startComposition(),
    endComposition: () => guardRef.current.endComposition(),
    shouldSubmitKey: (event: SubmitKeyEvent) =>
      guardRef.current.shouldSubmitKey(event),
    commitCompositionBeforePointerSubmit: () => {
      if (guardRef.current.shouldCommitBeforeButtonSubmit()) {
        inputRef.current?.blur();
      }
    },
    requestSubmit,
  };
}
