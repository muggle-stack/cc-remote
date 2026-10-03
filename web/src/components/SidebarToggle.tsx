import { useSyncExternalStore, type ReactNode } from "react";
import { sidebarOpenFeedback } from "../sidebar-feedback";

const MOBILE_TOUCH = "(max-width: 979px) and (any-pointer: coarse)";
const subscribe = (notify: () => void) => {
  const media = window.matchMedia(MOBILE_TOUCH);
  media.addEventListener("change", notify);
  return () => media.removeEventListener("change", notify);
};
const nativeSwitchAvailable = () => typeof navigator.vibrate !== "function"
  && window.matchMedia(MOBILE_TOUCH).matches
  && "switch" in document.createElement("input");
const serverSnapshot = () => false;
const markSwitch = (input: HTMLInputElement | null) => input?.setAttribute("switch", "");

/** Safari's feedback needs a real user-operated switch, not a scripted click.
 * Its checked state represents sidebar visibility, including swipe changes.
 * https://webkit.org/blog/15865/webkit-features-in-safari-18-0/
 */
export function SidebarToggle({ open, onOpenChange, className = "", children, testId }: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  className?: string;
  children: ReactNode;
  testId?: string;
}) {
  const native = useSyncExternalStore(subscribe, nativeSwitchAvailable, serverSnapshot);
  if (native) return <label className={`sidebar-native-toggle ${className}`} data-testid={testId}>
    <input ref={markSwitch} type="checkbox" role="switch" checked={open}
      aria-label="显示会话侧栏" onChange={event => onOpenChange(event.currentTarget.checked)} />
    {children}
  </label>;
  return <button type="button" className={className} data-testid={testId}
    aria-label="显示会话侧栏" aria-expanded={open}
    onClick={() => { if (!open) sidebarOpenFeedback(); onOpenChange(!open); }}>
    {children}
  </button>;
}
