import { useCallback, useEffect, useState } from "react";
import { type SlewPresetsResponse, fetchSlewPresets } from "../api/slewPresets";

interface UseSlewPresetsResult {
  data: SlewPresetsResponse | null;
  error: Error | null;
  refresh: () => void;
}

// Module-level per-camera cache — same shape/purpose as useZone's
// zoneCache. Prevents the panel from flashing empty on camera swap.
const cache = new Map<string, SlewPresetsResponse>();

/**
 * Poll slew.<camera>.presets. Slow interval (10s) — the polygon set
 * changes only on operator save. Poll exists so a save from another
 * tab converges without a hard refresh.
 */
export function useSlewPresets(camera: string, intervalMs = 10_000): UseSlewPresetsResult {
  const [data, setData] = useState<SlewPresetsResponse | null>(() =>
    camera ? (cache.get(camera) ?? null) : null,
  );
  const [error, setError] = useState<Error | null>(null);
  const [refreshTick, setRefreshTick] = useState(0);
  const refresh = useCallback(() => setRefreshTick((n) => n + 1), []);

  // biome-ignore lint/correctness/useExhaustiveDependencies: refreshTick is the intentional re-fire trigger
  useEffect(() => {
    if (!camera) {
      setData(null);
      return;
    }
    setData(cache.get(camera) ?? null);
    let cancelled = false;
    const controller = new AbortController();
    async function tick(): Promise<void> {
      try {
        const meta = await fetchSlewPresets(camera, controller.signal);
        if (cancelled) return;
        cache.set(camera, meta);
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
