import { useEffect, useState } from "react";
import { type BaselineMeta, fetchBaselineMeta } from "../api/baseline";

interface UseBaselineMetaResult {
  data: BaselineMeta | null;
  error: Error | null;
}

/**
 * Poll a single camera's baseline metadata. Same abort-on-unmount
 * discipline as useAlerts/useStatus. 3s interval — fresh enough to
 * pick up an operator's "Cap day"/"Cap night" click near-instantly
 * without hammering the disk-stat call.
 */
export function useBaselineMeta(camera: string, intervalMs = 3000): UseBaselineMetaResult {
  const [data, setData] = useState<BaselineMeta | null>(null);
  const [error, setError] = useState<Error | null>(null);

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
  }, [camera, intervalMs]);

  return { data, error };
}
