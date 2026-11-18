import * as renderer from "@aiden0z/pptx-renderer";
import DOMPurify from "dompurify";

import { installPresentationPreview } from "./presentation-preview";
import { sanitizeSvgMarkup } from "./safe-svg";

async function inspectSvg(event: MessageEvent) {
  const port = event.ports[0];
  if (event.source !== parent || event.data?.type !== "cc-remote-inspect-svg"
      || !port || !(event.data.bytes instanceof ArrayBuffer)) return;

  let url: string | undefined;
  try {
    const text = new TextDecoder("utf-8", { fatal: true }).decode(event.data.bytes);
    const svg = sanitizeSvgMarkup(text, DOMPurify);
    url = URL.createObjectURL(new Blob([svg], { type: "image/svg+xml" }));
    const image = new Image();
    image.id = "inspect-svg";
    image.style.cssText = "display:block;max-width:100vw;max-height:100vh;object-fit:contain";
    image.src = url;
    await image.decode();
    document.getElementById("slide")!.replaceChildren(image);
    port.postMessage({ type: "inspection" });
  } catch (error) {
    const message = error instanceof Error ? error.message : "Unable to render the SVG.";
    port.postMessage({ type: "inspection", error: message });
  } finally {
    if (url) URL.revokeObjectURL(url);
    port.close();
  }
}

// The iframe must remain opaque even though this trusted bundle can run scripts.
try {
  void parent.document;
} catch {
  installPresentationPreview(renderer);
  window.addEventListener("message", event => { void inspectSvg(event); });
  parent.postMessage({ type: "cc-remote-presentation-ready" }, "*");
}
