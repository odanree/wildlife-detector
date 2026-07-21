import { useCallback, useEffect, useState } from "react";
import { type ZoneMeta, fetchZone } from "../api/zone";

interface UseZoneResult {
  data: ZoneMeta | null;
  error: Error | null;
  refresh: () => void;
}

/**
 * Poll the current zone polygon for a camera. Same abort-on-unmount
 * shape as useAlerts/useStatus. Slow interval (10s) because the
 * polygon rarely changes — polling exists so a save from a second
 * browser tab converges without a hard refresh.
 */
export function useZone(camera: string, intervalMs = 10_000): UseZoneResult {
  const [data, setData] = useState<ZoneMeta | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [refreshTick, setRefreshTick] = useState(0);

  const refresh = useCallback(() => setRefreshTick((n) => n + 1), []);

  // biome-ignore lint/correctness/useExhaustiveDependencies: refreshTick is the intentional re-fire trigger for save() to force an immediate refetch
  useEffect(() => {
    if (!camera) return;
    let cancelled = false;
    const controller = new AbortController();

    async function tick(): Promise<void> {
      try {
        const meta = await fetchZone(camera, controller.signal);
        if (cancelled) return;
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
