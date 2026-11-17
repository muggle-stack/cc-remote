import * as renderer from "@aiden0z/pptx-renderer";
import { installPresentationPreview } from "./presentation-preview";

// The iframe must remain opaque even though this trusted bundle can run scripts.
try {
  void parent.document;
} catch {
  installPresentationPreview(renderer);
  parent.postMessage({ type: "cc-remote-presentation-ready" }, "*");
}
