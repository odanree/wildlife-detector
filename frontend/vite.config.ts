import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Flask serves the built bundle under /react/* (see src/web/preview.py).
// `base` must match so absolute asset URLs in index.html resolve correctly
// in the deployed container. Local `vite dev` uses the plugin proxy to
// forward /api/* + /status + /snapshots to the Flask backend on :8100 so
// components can call the real API during development.
export default defineConfig({
  plugins: [react()],
  base: "/react/",
  build: {
    outDir: "dist",
    emptyOutDir: true,
    sourcemap: true,
  },
  server: {
    port: 5173,
    proxy: {
      "/status": "http://localhost:8100",
      "/api": "http://localhost:8100",
      "/snapshots": "http://localhost:8100",
      "/stream.mjpg": "http://localhost:8100",
    },
  },
});
