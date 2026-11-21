import { defineConfig } from "vite";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { presentationDocument } from "./src/presentation-preview";

function wordDocument(script: string) {
  const modules = resolve(import.meta.dirname, "node_modules");
  const notices = ["docx-preview/LICENSE", "jszip/LICENSE.markdown"].map(file =>
    readFileSync(resolve(modules, file), "utf8")).join("\n\n");
  return `<!doctype html><!-- ${notices.replaceAll("-->", "--&gt;")} --><html><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; img-src data: blob:; style-src 'unsafe-inline'; font-src data: blob:; form-action 'none'; base-uri 'none'">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>html,body{margin:0;color-scheme:light}#document{min-width:max-content}.word-wrapper{min-height:100vh}</style>
</head><body><div id="styles"></div><main id="document"></main>
<script>${script.replace(/<\/script/gi, "<\\/script")}</script></body></html>`;
}

export default defineConfig(({ mode }) => ({
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
        this.emitFile({ type: "asset", fileName: mode === "word" ? "word-preview.html" : "presentation-preview.html",
          source: mode === "word" ? wordDocument(item.code) : presentationDocument(item.code) });
        delete bundle[name];
      }
    },
  }],
  build: {
    emptyOutDir: false,
    outDir: "public",
    lib: {
      entry: resolve(import.meta.dirname, mode === "word" ? "src/word-preview-runner.ts" : "src/presentation-preview-runner.ts"),
      name: "CCRemotePresentationPreview",
      formats: ["iife"],
      fileName: () => "cc-remote-presentation-preview.js",
    },
  },
}));
