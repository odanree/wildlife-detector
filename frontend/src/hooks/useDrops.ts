import { useEffect, useState } from "react";
import { type DropsQuery, type DropsResponse, fetchDrops } from "../api/drops";

interface UseDropsResult {
  data: DropsResponse | null;
  error: Error | null;
  loading: boolean;
  refresh: () => void;
}

/**
 * Fetch pre-VLM drops with the same latest-wins pattern as useAlerts
 * — per-refetch AbortController so paginating fast doesn't pile up
 * concurrent /api/drops requests eating the browser's ~6-per-host
 * HTTP/1.1 connection budget (see PR #145 fix that hit exactly this
 * on the alerts page).
 */
export function useDrops(query: DropsQuery = {}): UseDropsResult {
  const [data, setData] = useState<DropsResponse | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [refreshTick, setRefreshTick] = useState(0);

  const key = JSON.stringify(query);

  // biome-ignore lint/correctness/useExhaustiveDependencies: key is the JSON-stringified query, refreshTick is the intentional re-fire trigger
  useEffect(() => {
    let cancelled = false;
    let inflight: AbortController | null = null;
    const parsed = JSON.parse(key) as DropsQuery;

    async function refetch() {
      inflight?.abort();
      const ctrl = new AbortController();
      inflight = ctrl;
      try {
        const resp = await fetchDrops(parsed, ctrl.signal);
        if (cancelled || ctrl.signal.aborted) return;
        setData(resp);
        setError(null);
      } catch (e) {
        if (cancelled || ctrl.signal.aborted) return;
        if (e instanceof DOMException && e.name === "AbortError") return;
        setError(e instanceof Error ? e : new Error(String(e)));
      } finally {
        if (!cancelled && inflight === ctrl) {
          setLoading(false);
          inflight = null;
        }
      }
    }

    void refetch();

    return () => {
      cancelled = true;
      inflight?.abort();
    };
  }, [key, refreshTick]);

  return {
    data,
    error,
    loading,
    refresh: () => setRefreshTick((n) => n + 1),
  };
}
