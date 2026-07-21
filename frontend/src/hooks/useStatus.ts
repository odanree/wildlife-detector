import { useEffect, useState } from "react";
import { type StatusSnapshot, fetchStatus } from "../api/status";

interface UseStatusResult {
  data: StatusSnapshot | null;
  error: Error | null;
  loading: boolean;
}

/**
 * Poll /status on an interval. Aborts in-flight requests on unmount /
 * dep-change to avoid setState-on-unmounted warnings. Default interval
 * matches the vanilla-JS refresh cadence (1s) so header chips feel live
 * without hammering the backend.
 */
export function useStatus(camera?: string, intervalMs = 1000): UseStatusResult {
  const [data, setData] = useState<StatusSnapshot | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [loading, setLoading] = useState<boolean>(true);

  useEffect(() => {
    // Reset data on camera change — otherwise consumers keep reading
    // the previous camera's snapshot until the new fetch resolves,
    // which contaminates any downstream cache keyed by the *current*
    // camera (e.g. useDetectionSize) and causes cross-camera state
    // leaks in general.
    setData(null);
    setLoading(true);
    let cancelled = false;
    const controller = new AbortController();

    async function tick(): Promise<void> {
      try {
        const snap = await fetchStatus(camera, controller.signal);
        if (cancelled) return;
        setData(snap);
        setError(null);
      } catch (e) {
        if (cancelled) return;
        if (e instanceof DOMException && e.name === "AbortError") return;
        setError(e instanceof Error ? e : new Error(String(e)));
      } finally {
        if (!cancelled) setLoading(false);
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

  return { data, error, loading };
}
