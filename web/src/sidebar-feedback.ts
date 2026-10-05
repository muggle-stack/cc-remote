/** Call synchronously from the user's opening tap/touchend, never an effect. */
export function sidebarOpenFeedback(): void {
  if (typeof window === "undefined" || typeof navigator === "undefined"
      || !window.matchMedia("(max-width: 979px)").matches
      || navigator.maxTouchPoints < 1) return;
  // Haptics are optional: browser policy or OS settings may suppress them.
  try {
    navigator.vibrate?.(10);
  } catch {
    // Failure to provide feedback must never prevent opening the drawer.
  }
}
