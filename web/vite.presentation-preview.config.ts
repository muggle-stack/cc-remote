import { defineConfig } from "vite";
import { resolve } from "node:path";
import { presentationDocument } from "./src/presentation-preview";

export default defineConfig({
  publicDir: false,
  define: {
    "process.env.NODE_ENV": JSON.stringify("production"),
    // The classic sandbox bundle uses explicit options, not ESM asset lookup.
    "import.meta.resolve": "undefined",
  },
  plugins: [{
    name: "inline-presentation-preview",
    generateBundle(_options, bundle) {
      for (const [name, item] of Object.entries(bundle)) {
        if (item.type !== "chunk" || !item.isEntry) continue;
        this.emitFile({ type: "asset", fileName: "presentation-preview.html",
          source: presentationDocument(item.code) });
        delete bundle[name];
      }
    },
  }],
  build: {
    emptyOutDir: false,
    outDir: "public",
    lib: {
      entry: resolve(import.meta.dirname, "src/presentation-preview-runner.ts"),
      name: "CCRemotePresentationPreview",
      formats: ["iife"],
      fileName: () => "cc-remote-presentation-preview.js",
    },
  },
});
