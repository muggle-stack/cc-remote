import { useLayoutEffect } from "react";
import { clampPanelWidth } from "./responsive-layout";

// Retain the historical preview-panel preference for every right-side panel.
export const PANEL_WIDTH_KEY = "cc_remote_artifact_panel_width";
export const DESKTOP_PANEL_QUERY = "(min-width: 981px)";

/** Restore before the shell paints, including before a lazy panel is loaded. */
export function usePanelWidthPreference() {
  useLayoutEffect(() => {
    const root = document.documentElement;
    let saved = Number.NaN;
    try { saved = Number.parseFloat(localStorage.getItem(PANEL_WIDTH_KEY) || ""); }
    catch { /* A blocked preference store keeps the CSS default. */ }
    const fit = (width: number) => {
      if (window.matchMedia(DESKTOP_PANEL_QUERY).matches && Number.isFinite(width)) {
        root.style.setProperty("--panel-w", `${clampPanelWidth(width, window.innerWidth)}px`);
      }
    };
    fit(saved);
    const resize = () => {
      const current = Number.parseFloat(root.style.getPropertyValue("--panel-w"));
      fit(Number.isFinite(current) ? current : saved);
    };
    window.addEventListener("resize", resize);
    return () => window.removeEventListener("resize", resize);
  }, []);
}
