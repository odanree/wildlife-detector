// Prod-only React Profiler sink. Batches commit timing data and ships
// via navigator.sendBeacon so the payload survives page unload. The
// receiving Flask endpoint (POST /api/perf/profile in web_service.py)
// appends each row to a JSONL file for offline aggregation.
//
// Pattern: production telemetry probe with fire-and-forget delivery.
// sendBeacon is the right primitive here — it's designed for exactly
// this "commit-and-forget" shape, never blocks the render loop, and
// browsers guarantee delivery even during unload.
//
// Sampling: we only ship commits where actualDuration > SLOW_MS to
// keep the sink cheap. Everything faster than that is expected noise.
// Threshold is a knob; start conservative and tune based on what shows
// up in the first week of data.

const SLOW_MS = 16; // one frame at 60fps — anything slower is worth a look
const BATCH_SIZE = 20;
const FLUSH_INTERVAL_MS = 5_000;

export interface PerfEvent {
  id: string; // component id passed to <Profiler>
  phase: "mount" | "update" | "nested-update";
  actualDuration: number;
  baseDuration: number;
  startTime: number;
  commitTime: number;
}

// Commit SHA baked in at build time via vite define — see vite.config.ts.
// Lets the server-side aggregator group events by deploy without any
// runtime coordination.
declare const __COMMIT_SHA__: string;

let buffer: PerfEvent[] = [];
let flushTimer: ReturnType<typeof setTimeout> | null = null;

function flush(): void {
  if (buffer.length === 0) return;
  const payload = JSON.stringify({
    sha: typeof __COMMIT_SHA__ !== "undefined" ? __COMMIT_SHA__ : "dev",
    ua: navigator.userAgent,
    events: buffer,
  });
  buffer = [];
  if (flushTimer !== null) {
    clearTimeout(flushTimer);
    flushTimer = null;
  }
  // sendBeacon returns false if the payload is refused (too large or
  // browser denied). Fall back to a fire-and-forget fetch keepalive
  // so we don't silently lose the event. Neither path throws.
  const blob = new Blob([payload], { type: "application/json" });
  if (!navigator.sendBeacon?.("/api/perf/profile", blob)) {
    fetch("/api/perf/profile", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: payload,
      keepalive: true,
    }).catch(() => {
      // Sink is best-effort; a failed profile write must not affect
      // the operator's session in any observable way.
    });
  }
}

export function onProfilerCommit(
  id: string,
  phase: "mount" | "update" | "nested-update",
  actualDuration: number,
  baseDuration: number,
  startTime: number,
  commitTime: number,
): void {
  if (actualDuration < SLOW_MS) return;
  buffer.push({ id, phase, actualDuration, baseDuration, startTime, commitTime });
  if (buffer.length >= BATCH_SIZE) {
    flush();
    return;
  }
  if (flushTimer === null) {
    flushTimer = setTimeout(flush, FLUSH_INTERVAL_MS);
  }
}

// Flush on page hide — pagehide fires reliably on unload even on mobile
// where beforeunload doesn't. Uses the sendBeacon path in flush() which
// is specifically designed to succeed during navigation.
if (typeof window !== "undefined") {
  window.addEventListener("pagehide", flush);
}
