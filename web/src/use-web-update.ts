import { useCallback, useEffect, useRef, useState } from "react";
import { applyWebUpdate, availableWebBuild, WEB_BUILD, type WebBuild } from "./web-build";

/** Offer updates without interrupting reading or discarding in-memory input. */
export function useWebUpdate(enabled: boolean, connection: string, blocked: () => string | null) {
  const [build, setBuild] = useState<WebBuild | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [updating, setUpdating] = useState(false);
  const enabledRef = useRef(enabled);
  enabledRef.current = enabled;
  const reloadPending = useRef(false);
  const blockedRef = useRef(blocked);
  blockedRef.current = blocked;
  const checkRef = useRef<() => void>(() => {});
  const check = useCallback(() => checkRef.current(), []);
  useEffect(() => {
    if (!enabled || WEB_BUILD.build_id === "development") return;
    let disposed = false;
    let checking = false;
    const probe = async () => {
      if (checking || disposed) return;
      checking = true;
      const next = await availableWebBuild();
      checking = false;
      if (!disposed) setBuild(next);
    };
    const visible = () => { if (document.visibilityState === "visible") void probe(); };
    checkRef.current = () => { void probe(); };
    void probe();
    const timer = window.setInterval(visible, 60_000);
    window.addEventListener("focus", visible);
    window.addEventListener("online", visible);
    document.addEventListener("visibilitychange", visible);
    return () => {
      disposed = true;
      checkRef.current = () => {};
      window.clearInterval(timer);
      window.removeEventListener("focus", visible);
      window.removeEventListener("online", visible);
      document.removeEventListener("visibilitychange", visible);
    };
  }, [enabled, connection]);

  const reload = async () => {
    if (reloadPending.current || !enabledRef.current) return;
    reloadPending.current = true;
    setUpdating(true);
    // Recheck just before navigating: a rollout/rollback or newly typed draft
    // may have happened since the update notification first appeared.
    const reason = await applyWebUpdate(
      () => enabledRef.current ? blockedRef.current() : "请先登录。",
      () => window.location.reload(),
    );
    if (reason) {
      reloadPending.current = false;
      setMessage(reason);
      setUpdating(false);
    }
  };

  return build ? { id: build.build_id, version: build.version, message, updating, reload, check }
    : { id: null, version: null, message, updating, reload, check };
}
