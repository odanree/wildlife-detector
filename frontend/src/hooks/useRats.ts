import { useCallback, useEffect, useMemo, useState } from "react";
import { type RatSummary, type RatsQuery, type RatsResponse, fetchRats } from "../api/rats";

/**
 * Gallery filter state for /react/rats — the useAlertsFilters shape,
 * trimmed: every field is localStorage-persisted via a handler-owned
 * side effect (write in the setter, not a subscribing effect).
 *
 * Sort is applied client-side: the catalog is ~20 rows, so re-ordering
 * in a useMemo beats a server round-trip per click.
 */

export type RatsSort = "recent" | "active" | "tenured";
const SORT_VALUES: readonly RatsSort[] = ["recent", "active", "tenured"];

export interface RatsFiltersApi {
  camera: string;
  includeRetired: boolean;
  sort: RatsSort;
  setCamera: (v: string) => void;
  setIncludeRetired: (v: boolean) => void;
  setSort: (v: RatsSort) => void;
}

export function useRatsFilters(): RatsFiltersApi {
  const [camera, setCameraState] = useState<string>(
    () => localStorage.getItem("ratsCameraFilter") ?? "",
  );
  const [includeRetired, setIncludeRetiredState] = useState<boolean>(
    () => localStorage.getItem("ratsIncludeRetired") === "1",
  );
  const [sort, setSortState] = useState<RatsSort>(() => {
    const v = localStorage.getItem("ratsSort");
    return (SORT_VALUES as readonly string[]).includes(v ?? "") ? (v as RatsSort) : "recent";
  });

  const setCamera = useCallback((v: string) => {
    setCameraState(v);
    if (v) localStorage.setItem("ratsCameraFilter", v);
    else localStorage.removeItem("ratsCameraFilter");
  }, []);
  const setIncludeRetired = useCallback((v: boolean) => {
    setIncludeRetiredState(v);
    localStorage.setItem("ratsIncludeRetired", v ? "1" : "0");
  }, []);
  const setSort = useCallback((v: RatsSort) => {
    setSortState(v);
    localStorage.setItem("ratsSort", v);
  }, []);

  return { camera, includeRetired, sort, setCamera, setIncludeRetired, setSort };
}

export function sortRats(rats: RatSummary[], sort: RatsSort): RatSummary[] {
  const bySec = (iso: string) => Date.parse(iso) || 0;
  const out = [...rats];
  switch (sort) {
    case "active":
      out.sort((a, b) => b.alert_count - a.alert_count || a.id - b.id);
      break;
    case "tenured":
      out.sort((a, b) => bySec(a.first_seen) - bySec(b.first_seen) || a.id - b.id);
      break;
    default:
      out.sort((a, b) => bySec(b.last_seen) - bySec(a.last_seen) || a.id - b.id);
  }
  return out;
}

interface UseRatsResult {
  data: RatsResponse | null;
  /** Sorted per `sort`; empty while loading. */
  rats: RatSummary[];
  error: Error | null;
  loading: boolean;
  refresh: () => void;
  /** Local patch for optimistic writes (rename) — replaces the matching
   *  row in place; a later refresh reconciles with the server. */
  patchRat: (id: number, patch: Partial<RatSummary>) => void;
}

/**
 * One-shot fetch of /api/rats with latest-wins abort (same shape as
 * useDrops). Not SSE-driven: the catalog only changes on the nightly
 * clusterer run or an operator merge, both of which the UI already
 * knows to refresh after.
 */
export function useRats(query: RatsQuery = {}, sort: RatsSort = "recent"): UseRatsResult {
  const [data, setData] = useState<RatsResponse | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [refreshTick, setRefreshTick] = useState(0);

  const key = JSON.stringify(query);

  // biome-ignore lint/correctness/useExhaustiveDependencies: key is the JSON-stringified query, refreshTick is the intentional re-fire trigger
  useEffect(() => {
    let cancelled = false;
    let inflight: AbortController | null = null;
    const parsed = JSON.parse(key) as RatsQuery;

    async function refetch() {
      inflight?.abort();
      const ctrl = new AbortController();
      inflight = ctrl;
      try {
        const resp = await fetchRats(parsed, ctrl.signal);
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

  const rats = useMemo(() => sortRats(data?.rats ?? [], sort), [data, sort]);

  const patchRat = useCallback((id: number, patch: Partial<RatSummary>) => {
    setData((d) =>
      d ? { ...d, rats: d.rats.map((r) => (r.id === id ? { ...r, ...patch } : r)) } : d,
    );
  }, []);

  return {
    data,
    rats,
    error,
    loading,
    refresh: () => setRefreshTick((n) => n + 1),
    patchRat,
  };
}
