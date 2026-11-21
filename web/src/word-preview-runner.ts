import { renderAsync } from "docx-preview";
import { installPreviewEscape } from "./preview-keyboard";
import { installViewerZoom } from "./viewer-zoom";

let opaque = false;
try { void parent.document; } catch { opaque = true; }
if (opaque) {
  const receive = async (event: MessageEvent) => {
    const port = event.ports[0];
    if (event.source !== parent || event.data?.type !== "cc-remote-word"
        || !port || !(event.data.bytes instanceof ArrayBuffer)) return;
    window.removeEventListener("message", receive);
    try {
      const stage = document.getElementById("document")!;
      const style = document.getElementById("styles")!;
      await renderAsync(event.data.bytes, stage, style, {
        className: "word", inWrapper: true, ignoreLastRenderedPageBreak: false,
        renderAltChunks: false, renderComments: false, useBase64URL: true,
      });
      if (!stage.querySelector("section")) throw new Error("Missing document body");
      installPreviewEscape();
      installViewerZoom();
      port.postMessage({ type: "ready" });
    } catch {
      port.postMessage({ type: "error" });
    } finally {
      port.close();
    }
  };
  document.addEventListener("click", event => {
    if (event.target instanceof Element && event.target.closest("a")) event.preventDefault();
  }, true);
  window.addEventListener("message", receive);
}
