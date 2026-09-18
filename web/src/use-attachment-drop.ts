import { useEffect, useRef, useState } from "react";

/** The drop location owns the files, independently of keyboard/session focus. */
export function useAttachmentDrop(
  surface: "main" | "btw", disabled: boolean,
  onFiles: (files: FileList) => void,
): boolean {
  const latest = useRef({ disabled, onFiles });
  latest.current = { disabled, onFiles };
  const [over, setOver] = useState(false);
  useEffect(() => {
    const clear = () => setOver(false);
    const drag = (event: DragEvent) => {
      if (event.type === "dragleave" || event.type === "dragend") {
        if (!event.relatedTarget) clear();
        return;
      }
      if (!Array.from(event.dataTransfer?.types ?? []).includes("Files")) return;
      // Locked panes also suppress the browser's navigation to a dropped file.
      event.preventDefault();
      const side = event.target instanceof Element
        && !!event.target.closest('[data-attachment-target="btw"]');
      const active = surface === (side ? "btw" : "main") && !latest.current.disabled;
      if (event.type === "drop") {
        clear();
        if (active && event.dataTransfer?.files.length) latest.current.onFiles(event.dataTransfer.files);
      } else setOver(active);
    };
    const events = ["dragenter", "dragover", "dragleave", "drop", "dragend"] as const;
    for (const name of events) window.addEventListener(name, drag);
    window.addEventListener("blur", clear);
    return () => {
      for (const name of events) window.removeEventListener(name, drag);
      window.removeEventListener("blur", clear);
    };
  }, [surface]);
  return over && !disabled;
}
