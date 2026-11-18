import { cpSync, copyFileSync, mkdirSync } from "node:fs";
import { resolve } from "node:path";
import { defineConfig } from "vite";

const output = resolve(import.meta.dirname, "dist/artifact-inspection");
const modules = resolve(import.meta.dirname, "node_modules");

export default defineConfig({
  base: "./",
  publicDir: false,
  plugins: [{
    name: "bundle-artifact-renderer-resources",
    writeBundle() {
      for (const directory of ["cmaps", "standard_fonts"]) {
        cpSync(resolve(modules, "pdfjs-dist", directory), resolve(output, directory), { recursive: true });
      }
      const notices = resolve(output, "licenses");
      mkdirSync(notices, { recursive: true });
      for (const [dependency, file] of Object.entries({
        "pdfjs-dist": "LICENSE", "docx-preview": "LICENSE", jszip: "LICENSE.markdown",
        mermaid: "LICENSE", katex: "LICENSE",
      })) {
        copyFileSync(resolve(modules, dependency, file), resolve(notices, dependency + ".txt"));
      }
    },
  }],
  build: {
    outDir: output,
    emptyOutDir: true,
    // The opaque inspection frame loads modules only from the virtual bundle origin.
    modulePreload: { polyfill: false },
    rollupOptions: { input: resolve(import.meta.dirname, "artifact-document-preview.html") },
  },
});
