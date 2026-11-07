import { defineConfig } from "vite";
import { resolve } from "node:path";

// A classic, self-contained script works inside an opaque-origin frame without
// granting credentialed CORS to app assets. Parsers never enter the chat bundle.
export default defineConfig(({ mode }) => ({
  publicDir: false,
  build: {
    emptyOutDir: false,
    lib: {
      entry: resolve(import.meta.dirname, mode === "zoom" ? "src/viewer-zoom-entry.ts" : "src/viewer-runner/index.ts"),
      name: mode === "zoom" ? "CCRemoteViewerZoom" : "CCRemoteViewerRunner", formats: ["iife"],
      fileName: () => mode === "zoom" ? "cc-remote-viewer-zoom.js" : "cc-remote-viewer-runner.js",
    },
  },
}));
