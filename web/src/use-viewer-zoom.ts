import { useEffect, useRef, useState, type RefObject } from "react";

const INITIAL_ZOOM = { scale: 1, fit: true, ready: false };

export function useViewerZoom(
  frame: RefObject<HTMLIFrameElement | null>, instance: string | undefined,
  pageKey: string, origin = "null",
) {
  const [state, setState] = useState(INITIAL_ZOOM);
  const previous = useRef<{ key: string; fit: boolean; scale: number } | null>(null);
  const target = origin === "null" ? "*" : origin;
  const command = (action: "read" | "fit" | "reset" | "in" | "out") => {
    if (instance) frame.current?.contentWindow?.postMessage({ type: "cc-viewer-zoom-command", action }, target);
  };
  useEffect(() => {
    const saved = previous.current?.key === pageKey ? previous.current : null;
    setState(saved ? { scale: saved.scale, fit: saved.fit, ready: false } : INITIAL_ZOOM);
    if (!instance) return;
    let initialized = false;
    const listener = (event: MessageEvent) => {
      if (event.source !== frame.current?.contentWindow || event.origin !== origin) return;
      if (event.data?.type === "cc-viewer-zoom-ready") {
        initialized = true;
        frame.current?.contentWindow?.postMessage({ type: "cc-viewer-zoom-command",
          action: saved?.fit === false ? "set" : "fit", scale: saved?.scale }, target);
      } else if (initialized && event.data?.type === "cc-viewer-zoom-state" && typeof event.data.scale === "number"
          && Number.isFinite(event.data.scale) && event.data.scale >= 0.1 && event.data.scale <= 4
          && typeof event.data.fit === "boolean") {
        const next = { scale: event.data.scale, fit: event.data.fit };
        previous.current = { ...next, key: pageKey };
        setState({ ...next, ready: true });
      }
    };
    window.addEventListener("message", listener);
    return () => window.removeEventListener("message", listener);
  }, [frame, instance, pageKey, origin, target]);
  return { ...state, command };
}
