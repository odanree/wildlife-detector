import { useCallback, useEffect, useState } from "react";
import { type ZoneMeta, fetchZone } from "../api/zone";

interface UseZoneResult {
  data: ZoneMeta | null;
  error: Error | null;
  refresh: () => void;
}

/** Module-level cache of last-known zone snapshot per camera. Used to
 *  bridge the null-gap between a camera change and the first fresh
 *  fetch for the new camera — otherwise ZoneOverlay would flash empty
 *  (or flash the previous camera's polygon before the reset lands) on
 *  every promote-swap. */
const zoneCache = new Map<string, ZoneMeta>();

/**
 * Poll the current zone polygon for a camera. Same abort-on-unmount
 * shape as useAlerts/useStatus. Slow interval (10s) because the
 * polygon rarely changes — polling exists so a save from a second
 * browser tab converges without a hard refresh.
 *
 * Camera-scoped cache: `data` is reset to the cached snapshot (or null
 * if never seen) on camera change, and updated to `meta` on every
 * successful fetch. So on a promote-swap the ZoneOverlay renders the
 * correct polygon immediately from cache while the new /api/zone
 * request is in flight.
 */
export function useZone(camera: string, intervalMs = 10_000): UseZoneResult {
  const [data, setData] = useState<ZoneMeta | null>(() =>
    camera ? (zoneCache.get(camera) ?? null) : null,
  );
  const [error, setError] = useState<Error | null>(null);
  const [refreshTick, setRefreshTick] = useState(0);

  const refresh = useCallback(() => setRefreshTick((n) => n + 1), []);

  // biome-ignore lint/correctness/useExhaustiveDependencies: refreshTick is the intentional re-fire trigger for save() to force an immediate refetch
  useEffect(() => {
    if (!camera) {
      setData(null);
      return;
    }
    // Prime from cache immediately so overlays never flash empty on
    // camera change for a camera we've seen before.
    setData(zoneCache.get(camera) ?? null);

    let cancelled = false;
    const controller = new AbortController();

    async function tick(): Promise<void> {
      try {
        const meta = await fetchZone(camera, controller.signal);
        if (cancelled) return;
        zoneCache.set(camera, meta);
        setData(meta);
        setError(null);
      } catch (e) {
        if (cancelled) return;
        if (e instanceof DOMException && e.name === "AbortError") return;
        setError(e instanceof Error ? e : new Error(String(e)));
      }
    }

    void tick();
    const handle = window.setInterval(tick, intervalMs);
    return () => {
      cancelled = true;
      controller.abort();
      window.clearInterval(handle);
    };
  }, [camera, intervalMs, refreshTick]);

  return { data, error, refresh };
}
