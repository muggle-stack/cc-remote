import { resolve } from "node:path";
import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";
function reviewBundle(): Plugin {
  return {
    name: "theme-review-bundle",
    enforce: "pre",
    // Bridge eagerly walks the full lazy import graph. These two renderers
    // are unused by the fixed example conversation; omit them only here.
    resolveId(source, importer) {
      if (source === "mermaid") return "\0theme-review-diagrams";
      if (source === "./markdown-math-plugins" && importer?.endsWith("/src/markdown-math.ts"))
        return "\0theme-review-math";
    },
    load(id) {
      if (id === "\0theme-review-diagrams") return `export default {
        initialize() {},
        render() { throw new Error("主题预览不包含图表，请在正式会话中查看。"); }
      };`;
      if (id === "\0theme-review-math")
        return "export const remarkPlugins = []; export const rehypePlugins = [];";
    },
    generateBundle(_, bundle) {
      const files = Object.values(bundle);
      if (files.length > 6) this.error("Theme review must stay within six static resources.");
      for (const file of files) {
        const bytes = Buffer.byteLength(file.type === "chunk" ? file.code : file.source);
        if (bytes > 1536 * 1024) this.error(`Theme review resource exceeds 1.5 MiB: ${file.fileName}`);
      }
    },
  };
}

// Remote Viewer reads static files; it cannot execute the Vite/TSX dev entry.
// Keep this standalone review bundle separate from the deployed application.
export default defineConfig({
  plugins: [react(), reviewBundle()],
  base: "./",
  build: {
    outDir: "dist-theme-preview",
    emptyOutDir: true,
    copyPublicDir: false,
    modulePreload: false,
    cssCodeSplit: false,
    chunkSizeWarningLimit: 1536,
    rolldownOptions: {
      input: resolve(import.meta.dirname, "tests/theme-preview.html"),
      output: { codeSplitting: false },
    },
  },
});
