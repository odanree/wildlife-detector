import { useEffect, useMemo, useState } from "react";
import { fetchAlerts } from "../api/alerts";

/** Base key; camera scope (or "all") is suffixed onto it so per-camera
 *  watermarks don't leak across cameras. */
const SEEN_KEY_BASE = "alertsLastSeenTotal";
const SEEN_ID_KEY_BASE = "alertsLastSeenId";

/** Custom event fired on the same tab that just called markAlertsSeen,
 *  because the browser's native `storage` event only fires in OTHER tabs.
 *  useUnreadAlerts listens to both — this one for same-tab reactivity,
 *  the storage event for cross-tab sync. Detail carries the camera key
 *  and the new total so listeners don't have to re-read localStorage. */
const SEEN_UPDATED_EVENT = "wildlife-detector:alerts-seen-updated";
interface SeenUpdatedDetail {
  camera: string; // "all" or camera_id — the localStorage key suffix
  total: number;
}

function seenKey(camera?: string | null): string {
  return `${SEEN_KEY_BASE}:${camera || "all"}`;
}
function seenIdKey(camera?: string | null): string {
  return `${SEEN_ID_KEY_BASE}:${camera || "all"}`;
}

/** Read the last-seen alert id for a specific camera (or "all" if
 *  omitted). Returns null if never written. Exported so AlertsPage
 *  can snapshot at mount BEFORE markAlertsSeen rolls the watermark
 *  forward for row highlighting. */
export function readLastSeenId(camera?: string | null): number | null {
  try {
    const raw = localStorage.getItem(seenIdKey(camera));
    return raw === null ? null : Number.parseInt(raw, 10) || 0;
  } catch {
    return null;
  }
}

/**
 * Poll alert counts for one OR MORE cameras and sum unread across all
 * of them. The header badge in dual-pane view uses this to cover both
 * visible cameras so activity on either shows up in the count
 * regardless of which is primary.
 *
 * Single camera: fetches /api/alerts?limit=1&camera=<id>.
 * Multi-camera: fetches /api/alerts/counts once — batch response with
 * per-camera totals in one request. Sums unread per camera against
 * its per-camera watermark.
 *
 * Watermarks are per-camera: visiting /alerts?camera=yard clears
 * yard's unread contribution but leaves rooftop's alone.
 * Cold-start-per-camera prevents "99+" for historical alerts.
 *
 * Pattern: monotonic-counter diff with union-scoped aggregation.
 * The scope is a SET of cameras (visible in the panes), not a single
 * entity — badge = Σ per-camera unread.
 */
export function useUnreadAlerts(
  cameras?: readonly string[] | null,
  intervalMs = 5000,
): { unread: number } {
  // Normalize: empty / undefined → cross-camera pseudo-scope "all".
  const camsKey = cameras && cameras.length > 0 ? cameras.join(",") : "all";
  const cams = useMemo(() => camsKey.split(","), [camsKey]);

  const [totals, setTotals] = useState<Record<string, number>>({});
  const [seens, setSeens] = useState<Record<string, number | null>>(() => readWatermarks(cams));

  // Cameras change → re-read the new set's watermarks. Clear totals so
  // the unread computation doesn't briefly mix old-set numbers with
  // new-set state.
  // biome-ignore lint/correctness/useExhaustiveDependencies: cams identity tracked via camsKey
  useEffect(() => {
    setSeens(readWatermarks(cams));
    setTotals({});
  }, [camsKey]);

  // Server-pushed counts via SSE — one persistent EventSource connection
  // per tab. Replaces the previous per-tab setInterval that hit
  // /api/alerts/counts every intervalMs. Server-side polls the DB at a
  // fixed rate and fans out to all subscribers only on change, so DB
  // load is O(1) regardless of connected tabs and no HTTP requests
  // clutter DevTools per tick.
  //
  // The `all`-only single-camera case is a UI convention (no explicit
  // camera list → cross-camera pseudo-scope). SSE always returns
  // per-camera counts; we sum manually for the "all" case below.
  //
  // Cross-tab watermark sync stays on the `storage` event — that's a
  // browser-native pub-sub across same-origin tabs; nothing to do with
  // SSE.
  // biome-ignore lint/correctness/useExhaustiveDependencies: cams identity tracked via camsKey
  useEffect(() => {
    let cancelled = false;
    // AbortController only used for the fallback fetch path if SSE fails.
    const controller = new AbortController();

    // Cold-start helper — mutates seens map in place if the given
    // counts snapshot fills in a null watermark for a camera we care
    // about. Called from both the SSE onmessage handler and the
    // fetch-fallback handler so the logic doesn't duplicate.
    function coldStartSeens(newTotals: Record<string, number>): void {
      setSeens((prev) => {
        const next: Record<string, number | null> = { ...prev };
        let mutated = false;
        for (const c of cams) {
          if (next[c] === null && newTotals[c] != null) {
            try {
              localStorage.setItem(seenKey(c), String(newTotals[c]));
            } catch {
              /* ignore */
            }
            next[c] = newTotals[c];
            mutated = true;
          }
        }
        return mutated ? next : prev;
      });
    }

    // Normalize server-side counts (all cameras) to the shape this
    // hook publishes (subset filtered to `cams`, "all" as sum).
    function projectCounts(serverCounts: Record<string, number>): Record<string, number> {
      if (cams.length === 1 && cams[0] === "all") {
        return { all: Object.values(serverCounts).reduce((s, n) => s + n, 0) };
      }
      const out: Record<string, number> = {};
      for (const c of cams) out[c] = serverCounts[c] ?? 0;
      return out;
    }

    // Try SSE first — the modern path. If EventSource is unavailable or
    // errors, fall back to one-shot fetch per intervalMs (the pre-SSE
    // behavior) so the badge still updates.
    let es: EventSource | null = null;
    let fallbackHandle: number | null = null;

    function startFallbackPolling(): void {
      async function tick(): Promise<void> {
        try {
          const raw =
            cams.length === 1 && cams[0] === "all"
              ? await fetchAllTotal(controller.signal)
              : await fetchCountsForCameras(cams, controller.signal);
          if (cancelled) return;
          setTotals(raw);
          coldStartSeens(raw);
        } catch (e) {
          if (cancelled) return;
          if (e instanceof DOMException && e.name === "AbortError") return;
          // Silent — header badge failure isn't worth surfacing.
        }
      }
      void tick();
      fallbackHandle = window.setInterval(tick, intervalMs);
    }

    if (typeof EventSource !== "undefined") {
      es = new EventSource("/api/alerts/events");
      es.onmessage = (ev) => {
        if (cancelled) return;
        try {
          const msg = JSON.parse(ev.data) as { type?: string; counts?: Record<string, number> };
          if (msg.type !== "counts" || !msg.counts) return;
          const projected = projectCounts(msg.counts);
          setTotals(projected);
          coldStartSeens(projected);
        } catch {
          /* malformed frame — skip */
        }
      };
      es.onerror = () => {
        // Auto-reconnect is EventSource's job for transient errors; only
        // fall back to polling if the connection outright fails (state
        // stays CLOSED). We check on next tick.
        if (es && es.readyState === EventSource.CLOSED && fallbackHandle === null) {
          startFallbackPolling();
        }
      };
    } else {
      startFallbackPolling();
    }

    // Cross-tab sync: any tab stamping a watermark for one of our
    // cameras should update our seen state so the badge stays honest.
    const watchedKeys = new Set(cams.map((c) => seenKey(c)));
    function onStorage(e: StorageEvent) {
      if (!e.key || !watchedKeys.has(e.key) || e.newValue === null) return;
      const camera = e.key.slice(SEEN_KEY_BASE.length + 1);
      setSeens((prev) => ({
        ...prev,
        [camera]: Number.parseInt(e.newValue as string, 10) || 0,
      }));
    }
    window.addEventListener("storage", onStorage);

    // Same-tab sync: `storage` events don't fire in the tab that wrote
    // localStorage. AlertsPage calls markAlertsSeen() during its
    // effect chain and expects the badge in THIS tab's header to
    // update. The custom event fills that gap — same shape as
    // onStorage but for the same tab.
    const watchedCameras = new Set(cams);
    function onSeenUpdated(e: Event) {
      const detail = (e as CustomEvent<SeenUpdatedDetail>).detail;
      if (!detail || !watchedCameras.has(detail.camera)) return;
      setSeens((prev) => ({ ...prev, [detail.camera]: detail.total }));
    }
    window.addEventListener(SEEN_UPDATED_EVENT, onSeenUpdated);

    return () => {
      cancelled = true;
      controller.abort();
      es?.close();
      if (fallbackHandle != null) window.clearInterval(fallbackHandle);
      window.removeEventListener("storage", onStorage);
      window.removeEventListener(SEEN_UPDATED_EVENT, onSeenUpdated);
    };
  }, [camsKey, intervalMs]);

  const unread = cams.reduce((sum, c) => {
    const s = seens[c];
    const t = totals[c] ?? 0;
    return sum + (s === null ? 0 : Math.max(0, t - s));
  }, 0);

  return { unread };
}

function readWatermarks(cams: readonly string[]): Record<string, number | null> {
  const out: Record<string, number | null> = {};
  for (const c of cams) {
    try {
      const raw = localStorage.getItem(seenKey(c));
      out[c] = raw === null ? null : Number.parseInt(raw, 10) || 0;
    } catch {
      out[c] = null;
    }
  }
  return out;
}

async function fetchAllTotal(signal: AbortSignal): Promise<Record<string, number>> {
  const r = await fetchAlerts({ limit: 1 }, signal);
  return { all: r.total ?? 0 };
}

/** Fetch per-camera counts via /api/alerts/counts. Restricts response
 *  to cameras we care about so unrelated activity doesn't leak into
 *  our sum. */
async function fetchCountsForCameras(
  cams: readonly string[],
  signal: AbortSignal,
): Promise<Record<string, number>> {
  const r = await fetch("/api/alerts/counts", { signal });
  if (!r.ok) throw new Error(`/api/alerts/counts ${r.status}`);
  const body = (await r.json()) as Record<string, number>;
  const out: Record<string, number> = {};
  for (const c of cams) out[c] = body[c] ?? 0;
  return out;
}

/**
 * Mark alerts as seen for a specific camera (or "all"). Writes both
 * watermarks:
 *   - total watermark → header badge unread count
 *   - highest-id watermark → alerts-page row highlighting
 *
 * Camera scope matches useUnreadAlerts. Call from AlertsPage after
 * data arrives with the same camera filter the page is showing.
 */
export function markAlertsSeen(
  camera: string | null | undefined,
  total: number,
  highestId?: number,
): void {
  try {
    localStorage.setItem(seenKey(camera), String(total));
    if (highestId != null) {
      localStorage.setItem(seenIdKey(camera), String(highestId));
    }
    // Same-tab notification — the native `storage` event won't fire on
    // this tab, only on other tabs. Custom event fills that gap so the
    // header badge updates immediately when the operator visits the
    // alerts page.
    const detail: SeenUpdatedDetail = { camera: camera || "all", total };
    window.dispatchEvent(new CustomEvent(SEEN_UPDATED_EVENT, { detail }));
  } catch {
    /* ignore */
  }
}
