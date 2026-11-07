import { useEffect, useState, type RefObject } from "react";

type ZoomGrant = { id: string; mode: "bridge" | "isolated"; origin?: string };
export function useViewerZoom(frame: RefObject<HTMLIFrameElement | null>, grant: ZoomGrant | null) {
  const [state, setState] = useState({ scale: 1, fit: false, ready: false });
  const origin = grant?.mode === "isolated" ? grant.origin : "null";
  const command = (action: "read" | "fit" | "reset" | "in" | "out") => {
    if (!grant) return;
    frame.current?.contentWindow?.postMessage({ type: "cc-viewer-zoom-command", action },
      origin === "null" ? "*" : origin!);
  };
  useEffect(() => {
    setState({ scale: 1, fit: false, ready: false });
    if (!grant) return;
    const listener = (event: MessageEvent) => {
      if (event.source !== frame.current?.contentWindow || event.origin !== origin) return;
      if (event.data?.type === "cc-viewer-zoom-ready") {
        frame.current?.contentWindow?.postMessage({ type: "cc-viewer-zoom-command", action: "read" },
          origin === "null" ? "*" : origin!);
      } else if (event.data?.type === "cc-viewer-zoom-state" && typeof event.data.scale === "number"
          && Number.isFinite(event.data.scale) && event.data.scale >= 0.1 && event.data.scale <= 4
          && typeof event.data.fit === "boolean") {
        setState({ scale: event.data.scale, fit: event.data.fit, ready: true });
      }
    };
    window.addEventListener("message", listener);
    return () => window.removeEventListener("message", listener);
  }, [frame, grant, origin]);
  return { ...state, command };
}
