import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { createHash } from "node:crypto";
import { readFileSync, readdirSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = dirname(fileURLToPath(import.meta.url));
const digest = createHash("sha256");
function hashInput(path: string) {
  digest.update(path).update("\0");
  digest.update(readFileSync(join(root, path))).update("\0");
}
function hashTree(path: string) {
  const entries = readdirSync(join(root, path), { withFileTypes: true })
    .sort((a, b) => a.name < b.name ? -1 : a.name > b.name ? 1 : 0);
  for (const entry of entries) {
    const child = `${path}/${entry.name}`;
    if (entry.isDirectory()) hashTree(child);
    else if (entry.isFile()) hashInput(child);
  }
}
hashTree("src");
hashTree("public");
for (const path of ["index.html", "package-lock.json", "vite.config.ts", "tsconfig.app.json"]) hashInput(path);
const webBuild = {
  ...JSON.parse(readFileSync(join(root, "public/cc-remote-build.json"), "utf8")),
  build_id: digest.digest("hex"),
};

// Dev: proxy /api + /ws to the local relay so the browser can use a same-origin
// WebSocket (and we avoid CORS). Production: the relay serves the built
// web/dist on the same origin, so /ws is same-origin naturally.
export default defineConfig({
  define: { __CC_REMOTE_WEB_BUILD__: JSON.stringify(webBuild) },
  plugins: [react(), {
    name: "cc-remote-build-identity",
    writeBundle(options) {
      writeFileSync(join(options.dir ?? join(root, "dist"), "cc-remote-build.json"),
        JSON.stringify(webBuild, null, 2) + "\n");
    },
  }],
  build: {
    // Mermaid's generated parser is a single ~663 kB lazy module. Keep Vite's
    // generic warning above that known boundary; the actual startup path is
    // enforced separately by scripts/check-bundle-budget.mjs.
    chunkSizeWarningLimit: 700,
    rolldownOptions: {
      output: {
        // Keep stable framework and parser code in content-addressed chunks so
        // normal cc-remote releases do not make clients download and parse the
        // whole application bundle again. Only initial dependencies participate;
        // Mermaid/KaTeX keep their existing on-demand boundaries.
        codeSplitting: {
          groups: [
            {
              name: "react-runtime",
              test: /node_modules[\\/](?:react|react-dom|scheduler)[\\/]/,
              tags: ["$initial"],
              priority: 20,
            },
            {
              name: "initial-vendor",
              // Keep relay delivery, reducer and projection primitives together.
              // This compresses their common field names without increasing
              // startup requests or pulling any lazy UI into the initial load.
              test: /node_modules[\\/]|preload-helper|src[\\/](?:compaction-orphans|history-browse|history-requests|history-detail-projection|reducer|runtime-bounds|remote-viewer|file-changes|tool-command|use-link-popover|web-link-url|ws)\.ts$|src[\\/]icons\.tsx$/,
              tags: ["$initial"],
              priority: 10,
            },
          ],
        },
      },
    },
  },
  server: {
    proxy: {
      "/api": { target: "http://127.0.0.1:8765" },
      "/ws": {
        target: "ws://127.0.0.1:8765",
        ws: true,
        // Dev pages originate on :5173. Rewrite only on this loopback proxy so
        // the relay can keep exact PUBLIC_ORIGIN validation enabled.
        rewriteWsOrigin: true,
      },
    },
  },
});
