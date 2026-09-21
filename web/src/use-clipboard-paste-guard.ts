import { useEffect, useRef, type RefObject } from "react";
import { ClipboardPasteGuard } from "./clipboard-paste-guard";

function isIOS(): boolean {
  return typeof navigator !== "undefined" && (/iPad|iPhone|iPod/.test(navigator.userAgent)
    || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1));
}

export function useClipboardPasteGuard(
  input: RefObject<HTMLTextAreaElement | null>, scope: string,
): ClipboardPasteGuard {
  const guard = useRef<ClipboardPasteGuard | null>(null);
  guard.current ??= new ClipboardPasteGuard(isIOS());
  const previousScope = useRef(scope);
  if (previousScope.current !== scope) {
    previousScope.current = scope;
    guard.current.clear();
  }
  useEffect(() => {
    if (!isIOS()) return;
    const current = guard.current!;
    const beforeInput = (event: Event) => {
      if (event.target !== input.current) return;
      const edit = event as InputEvent;
      if (edit.isComposing) { current.clear(); return; }
      if (edit.cancelable && current.blocksNativeInsert(input.current!, edit.inputType, edit.data)) {
        edit.preventDefault();
      }
    };
    const changed = (event: Event) => {
      if (event.target !== input.current) return;
      const edit = event as InputEvent;
      current.observeInput(input.current!, edit.inputType, edit.data);
    };
    // Listen natively: React's onBeforeInput abstraction does not expose all
    // WebKit InputEvent paths. Delegation also handles a replaced textarea ref.
    document.addEventListener("beforeinput", beforeInput, true);
    document.addEventListener("input", changed, true);
    const focusOut = (event: Event) => {
      // New-chat temporarily disables its textarea during attachment import.
      // That programmatic blur is not a second user action.
      if (event.target === input.current && input.current?.disabled) return;
      current.clear();
    };
    const boundaries = ["pointerdown", "touchstart", "keydown", "compositionstart", "copy", "cut"] as const;
    for (const name of boundaries) document.addEventListener(name, current.clear, true);
    document.addEventListener("focusout", focusOut, true);
    window.addEventListener("blur", current.clear);
    return () => {
      document.removeEventListener("beforeinput", beforeInput, true);
      document.removeEventListener("input", changed, true);
      for (const name of boundaries) document.removeEventListener(name, current.clear, true);
      document.removeEventListener("focusout", focusOut, true);
      window.removeEventListener("blur", current.clear);
      current.clear();
    };
  }, [input]);
  return guard.current;
}
