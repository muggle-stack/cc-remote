import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev: proxy /api + /ws to the local relay so the browser can use a same-origin
// WebSocket (and we avoid CORS). Production: the relay serves the built
// web/dist on the same origin, so /ws is same-origin naturally.
export default defineConfig({
  plugins: [react()],
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
              // Keep small, shared projection primitives out of the entry
              // without adding a fifth startup request. They have no UI side
              // effects and change only with the bounded-history contract.
              test: /node_modules[\\/]|preload-helper|src[\\/](?:compaction-orphans|history-browse|history-requests|runtime-bounds|remote-viewer)\.ts$|src[\\/]icons\.tsx$/,
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
