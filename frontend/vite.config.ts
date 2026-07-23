import { execSync } from "node:child_process";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Bake the current git SHA into the bundle so the tier-3 profiler sink
// can attribute each perf event to the deploy that produced it. Falls
// back to "dev" if git isn't available at build time (e.g. inside a
// container without .git). See frontend/src/util/perfSink.ts.
const commitSha = (() => {
  try {
    return execSync("git rev-parse --short HEAD", { encoding: "utf8" }).trim();
  } catch {
    return "dev";
  }
})();

// Flask serves the built bundle under /react/* (see src/web/preview.py).
// `base` must match so absolute asset URLs in index.html resolve correctly
// in the deployed container. Local `vite dev` uses the plugin proxy to
// forward /api/* + /status + /snapshots to the Flask backend on :8100 so
// components can call the real API during development.
export default defineConfig({
  plugins: [react()],
  base: "/react/",
  define: {
    __COMMIT_SHA__: JSON.stringify(commitSha),
  },
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
