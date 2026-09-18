import { useCallback, useEffect, useLayoutEffect, useState } from "react";

const STORAGE_KEY = "cc_remote_bold_text";

function readBoldText(): boolean {
  try { return window.localStorage.getItem(STORAGE_KEY) === "1"; }
  catch { return false; }
}

/** A browser-wide reading preference, shared by every engine and theme. */
export function useBoldText() {
  const [boldText, setValue] = useState(readBoldText);

  useLayoutEffect(() => {
    document.documentElement.dataset.boldText = String(boldText);
  }, [boldText]);

  useEffect(() => {
    const update = (event: StorageEvent) => {
      if (event.key === STORAGE_KEY || event.key === null) setValue(readBoldText());
    };
    window.addEventListener("storage", update);
    return () => window.removeEventListener("storage", update);
  }, []);

  const setBoldText = useCallback((enabled: boolean) => {
    setValue(enabled);
    // Opaque preview frames and storage-blocked browsers still update in memory.
    try { window.localStorage.setItem(STORAGE_KEY, enabled ? "1" : "0"); }
    catch { /* This setting remains usable for the current page. */ }
  }, []);

  return { boldText, setBoldText };
}
