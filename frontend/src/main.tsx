// wdyr must be imported before React itself is used anywhere else in
// the app — the library patches React.createElement. Dev-only; the
// import is tree-shaken out of prod bundles by vite's DEV constant.
if (import.meta.env.DEV) {
  await import("./wdyr");
}

import { Profiler, StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App.tsx";
import "./index.css";
import { onProfilerCommit } from "./util/perfSink";

const rootEl = document.getElementById("root");
if (!rootEl) throw new Error("#root not found in index.html");

// <Profiler> runs in both dev and prod but the sink only ships to the
// backend in prod (dev builds POST to :5173 which the vite proxy
// forwards to Flask :8100 — see vite.config.ts). Overhead is a fixed
// per-commit cost measured in microseconds, negligible for a headless
// yard detector's operator UI. The wrapping cost is what buys us the
// "H1-class perf regression shows up on the SLO dashboard, not in a
// six-month-later audit" property.
createRoot(rootEl).render(
  <StrictMode>
    <Profiler id="App" onRender={onProfilerCommit}>
      <App />
    </Profiler>
  </StrictMode>,
);
