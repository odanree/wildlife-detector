import { useCallback, useEffect, useState } from "react";
import { type RatDetail, fetchRat } from "../api/rats";

interface UseRatResult {
  data: RatDetail | null;
  error: Error | null;
  loading: boolean;
  refresh: () => void;
  /** Local patch for optimistic writes (rename / merge-retire). The
   *  caller keeps the previous value and re-applies it on error. */
  patch: (p: Partial<RatDetail>) => void;
}

/**
 * Single-rat detail for /react/rats/:id. One fetch per id, latest-wins
 * abort on id change. Not polled: a rat's alert list only grows when
 * the nightly clusterer assigns new members, so a live SSE hookup here
 * would burn a connection for a once-a-day event.
 */
export function useRat(id: number | null): UseRatResult {
  const [data, setData] = useState<RatDetail | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [refreshTick, setRefreshTick] = useState(0);

  // biome-ignore lint/correctness/useExhaustiveDependencies: refreshTick is the intentional re-fire trigger
  useEffect(() => {
    if (id == null || !Number.isFinite(id)) {
      setData(null);
      setError(new Error("invalid rat id"));
      setLoading(false);
      return;
    }
    let cancelled = false;
    const ctrl = new AbortController();
    setLoading(true);
    fetchRat(id, ctrl.signal)
      .then((d) => {
        if (cancelled) return;
        setData(d);
        setError(null);
      })
      .catch((e) => {
        if (cancelled) return;
        if (e instanceof DOMException && e.name === "AbortError") return;
        setError(e instanceof Error ? e : new Error(String(e)));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
      ctrl.abort();
    };
  }, [id, refreshTick]);

  const patch = useCallback((p: Partial<RatDetail>) => {
    setData((d) => (d ? { ...d, ...p } : d));
  }, []);

  return {
    data,
    error,
    loading,
    refresh: () => setRefreshTick((n) => n + 1),
    patch,
  };
}
