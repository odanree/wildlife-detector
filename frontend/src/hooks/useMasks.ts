import { useCallback, useEffect, useState } from "react";
import { type MasksMeta, fetchMasks } from "../api/masks";

interface UseMasksResult {
  data: MasksMeta | null;
  error: Error | null;
  refresh: () => void;
}

/** Module-level cache of last-known masks snapshot per camera. Bridges
 *  the null-gap between a camera change and the first fetch for the
 *  new camera so MaskOverlay doesn't flash empty on promote-swap. */
const masksCache = new Map<string, MasksMeta>();

/**
 * Poll a camera's OSD masks. Same shape as useZone: 10s interval
 * (masks change rarely — polling exists for cross-tab convergence),
 * refresh() for save-then-refetch flow.
 *
 * Camera-scoped cache primes `data` from the last-known snapshot on
 * camera change so overlays render immediately without flashing.
 */
export function useMasks(camera: string, intervalMs = 10_000): UseMasksResult {
  const [data, setData] = useState<MasksMeta | null>(() =>
    camera ? (masksCache.get(camera) ?? null) : null,
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
    setData(masksCache.get(camera) ?? null);

    let cancelled = false;
    const controller = new AbortController();

    async function tick(): Promise<void> {
      try {
        const meta = await fetchMasks(camera, controller.signal);
        if (cancelled) return;
        masksCache.set(camera, meta);
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
