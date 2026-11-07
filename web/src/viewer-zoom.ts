/** Runs inside the preview sandbox in both Bridge and Isolated modes. */
export function installViewerZoom() {
  if (window === parent) return;
  const root = document.documentElement;
  const baseZoom = parseFloat(getComputedStyle(root).zoom) || 1;
  let scale = 1;
  let fit = false;
  let parentOrigin: string | null = null;
  let touch: { distance: number; scale: number } | null = null;
  let gesture: { scale: number; start: number } | null = null;
  let resizing = 0;
  const clamp = (value: number) => Math.max(0.1, Math.min(4, value));
  const center = () => ({ x: innerWidth / 2, y: innerHeight / 2 });
  const report = () => {
    if (parentOrigin) parent.postMessage({ type: "cc-viewer-zoom-state", scale, fit }, parentOrigin);
  };
  const apply = (value: number, point = center()) => {
    if (!Number.isFinite(value)) return;
    const next = clamp(value);
    const x = (scrollX + point.x) / scale, y = (scrollY + point.y) / scale;
    root.style.zoom = String(baseZoom * next);
    scale = next;
    // Native layout/hit testing stays intact, including sliders and canvas.
    // Use instant scrolling even when the source sets scroll-behavior:smooth.
    window.scrollTo({ left: x * scale - point.x, top: y * scale - point.y, behavior: "instant" });
    report();
  };
  const fitWidth = () => {
    if (!root.clientWidth) return; // A retained, hidden frame keeps its zoom.
    const x = scrollX, y = scrollY;
    root.style.zoom = String(baseZoom);
    // Measure at 100% synchronously, then paint only the final scale. Measuring
    // an already shrunken document cannot detect space to grow after docking.
    const width = Math.max(root.scrollWidth, document.body?.scrollWidth * baseZoom || 0);
    const next = clamp(Math.min(1, root.clientWidth / Math.max(1, width)));
    root.style.zoom = String(baseZoom * scale);
    window.scrollTo({ left: x, top: y, behavior: "instant" });
    apply(next, { x: 0, y: 0 });
  };
  window.addEventListener("message", (event) => {
    if (event.source !== parent || event.data?.type !== "cc-viewer-zoom-command") return;
    if (parentOrigin && event.origin !== parentOrigin) return;
    parentOrigin = event.origin;
    switch (event.data.action) {
      case "read": report(); break;
      case "fit": fit = true; fitWidth(); break;
      case "reset": fit = false; apply(1); break;
      case "in": fit = false; apply(scale * 1.2); break;
      case "out": fit = false; apply(scale / 1.2); break;
    }
  });
  // Prevent browser-level pinch from zooming the surrounding chat. Single
  // finger scrolling is still native; child controls may use touch-action:none.
  if (getComputedStyle(root).touchAction !== "none") root.style.touchAction = "pan-x pan-y";
  const ownsTouch = (event: Event) => {
    // Pointer-event viewers (e.g. OrbitControls) commonly claim gestures via
    // touch-action rather than cancelling TouchEvents. Leave those alone too.
    let target = event.target instanceof Element ? event.target : null;
    while (target) {
      if (getComputedStyle(target).touchAction === "none") return true;
      target = target.parentElement;
    }
    return false;
  };
  window.addEventListener("wheel", (event) => {
    if (!event.ctrlKey || event.defaultPrevented) return;
    event.preventDefault();
    if (touch || gesture) return;
    fit = false;
    const units = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? innerHeight : 1;
    apply(scale * Math.exp(-Math.max(-100, Math.min(100, event.deltaY * units)) * 0.01),
      { x: event.clientX, y: event.clientY });
  }, { passive: false });
  const touches = (event: TouchEvent) => {
    const a = event.touches[0], b = event.touches[1];
    return { distance: Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY),
      x: (a.clientX + b.clientX) / 2, y: (a.clientY + b.clientY) / 2 };
  };
  window.addEventListener("touchstart", (event) => {
    if (event.touches.length !== 2 || event.defaultPrevented || ownsTouch(event)) { touch = null; return; }
    event.preventDefault();
    const point = touches(event);
    touch = { distance: Math.max(1, point.distance), scale };
    gesture = null;
  }, { passive: false });
  window.addEventListener("touchmove", (event) => {
    if (!touch || event.touches.length !== 2 || event.defaultPrevented) return;
    event.preventDefault();
    fit = false;
    const point = touches(event);
    apply(touch.scale * point.distance / touch.distance, point);
  }, { passive: false });
  const endTouch = () => { touch = null; };
  window.addEventListener("touchend", endTouch);
  window.addEventListener("touchcancel", endTouch);
  type SafariGesture = Event & { scale: number; clientX: number; clientY: number };
  window.addEventListener("gesturestart", (raw) => {
    if (raw.defaultPrevented || ownsTouch(raw)) return;
    raw.preventDefault();
    const event = raw as SafariGesture;
    if (!touch && Number.isFinite(event.scale) && event.scale > 0) gesture = { scale, start: event.scale };
  }, { passive: false });
  window.addEventListener("gesturechange", (raw) => {
    if (raw.defaultPrevented) return;
    raw.preventDefault();
    if (!gesture || touch) return;
    const event = raw as SafariGesture;
    if (!Number.isFinite(event.scale) || event.scale <= 0) return;
    fit = false;
    apply(gesture.scale * (event.scale / gesture.start) ** 2,
      Number.isFinite(event.clientX) && Number.isFinite(event.clientY)
        ? { x: event.clientX, y: event.clientY } : center());
  }, { passive: false });
  window.addEventListener("gestureend", (event) => {
    if (gesture) event.preventDefault();
    gesture = null;
  });
  window.addEventListener("resize", () => {
    cancelAnimationFrame(resizing);
    resizing = requestAnimationFrame(() => { if (fit) fitWidth(); });
  });
  parent.postMessage({ type: "cc-viewer-zoom-ready" }, "*");
}
