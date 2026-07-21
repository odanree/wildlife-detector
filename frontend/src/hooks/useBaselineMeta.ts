import { useCallback, useEffect, useState } from "react";
import { type BaselineMeta, fetchBaselineMeta } from "../api/baseline";

interface UseBaselineMetaResult {
  data: BaselineMeta | null;
  error: Error | null;
  /** Force an immediate refetch — used by capture/clear callers so
   * the UI reflects the new state without waiting for the poll tick. */
  refresh: () => void;
}

/**
 * Poll a single camera's baseline metadata. 3s interval — fresh enough
 * to pick up an operator's "Cap day"/"Cap night" click near-instantly
 * without hammering the disk-stat call. `refresh()` is the escape hatch
 * for the capture/clear button flows to force an immediate update.
 */
export function useBaselineMeta(camera: string, intervalMs = 3000): UseBaselineMetaResult {
  const [data, setData] = useState<BaselineMeta | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [refreshTick, setRefreshTick] = useState(0);

  const refresh = useCallback(() => setRefreshTick((n) => n + 1), []);

  // biome-ignore lint/correctness/useExhaustiveDependencies: refreshTick is the intentional re-fire trigger for callers who need to force an immediate refetch; effect body doesn't read its value
  useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();

    async function tick(): Promise<void> {
      try {
        const meta = await fetchBaselineMeta(camera, controller.signal);
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
