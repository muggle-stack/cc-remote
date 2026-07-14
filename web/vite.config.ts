import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev: proxy /api + /ws to the local relay so the browser can use a same-origin
// WebSocket (and we avoid CORS). Production: the relay serves the built
// web/dist on the same origin, so /ws is same-origin naturally.
export default defineConfig({
  plugins: [react()],
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
