import { installViewerZoom } from "./viewer-zoom";

// Injected before source meta CSP; install after the page's own input handlers.
if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", () => setTimeout(installViewerZoom, 0), { once: true });
} else installViewerZoom();
