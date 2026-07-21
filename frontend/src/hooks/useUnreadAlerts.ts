import { useEffect, useMemo, useState } from "react";
import { fetchAlerts } from "../api/alerts";

/** Base key; camera scope (or "all") is suffixed onto it so per-camera
 *  watermarks don't leak across cameras. */
const SEEN_KEY_BASE = "alertsLastSeenTotal";
const SEEN_ID_KEY_BASE = "alertsLastSeenId";

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

  // biome-ignore lint/correctness/useExhaustiveDependencies: cams identity tracked via camsKey
  useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();

    async function tick(): Promise<void> {
      try {
        const newTotals =
          cams.length === 1 && cams[0] === "all"
            ? await fetchAllTotal(controller.signal)
            : await fetchCountsForCameras(cams, controller.signal);
        if (cancelled) return;
        setTotals(newTotals);
        // Cold-start per camera: adopt current total as seen on first
        // sighting so we don't show 99+ for historical alerts.
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
      } catch (e) {
        if (cancelled) return;
        if (e instanceof DOMException && e.name === "AbortError") return;
        // Silent — header badge failure isn't worth surfacing.
      }
    }

    void tick();
    const handle = window.setInterval(tick, intervalMs);

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

    return () => {
      cancelled = true;
      controller.abort();
      window.clearInterval(handle);
      window.removeEventListener("storage", onStorage);
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
  } catch {
    /* ignore */
  }
}
