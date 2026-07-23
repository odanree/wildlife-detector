import { Profiler, StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App.tsx";
import "./index.css";
import { onProfilerCommit } from "./util/perfSink";

const rootEl = document.getElementById("root");
if (!rootEl) throw new Error("#root not found in index.html");

// <Profiler> wraps App in both dev and prod. The sink batches slow
// commits and POSTs to /api/perf/profile — Flask appends them to
// data/perf-profile.jsonl for offline aggregation. See .audit/README.md
// for why tier 2 (why-did-you-render) was dropped in favor of the two
// tiers below.
createRoot(rootEl).render(
  <StrictMode>
    <Profiler id="App" onRender={onProfilerCommit}>
      <App />
    </Profiler>
  </StrictMode>,
);
