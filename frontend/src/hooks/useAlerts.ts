import { useEffect, useState } from "react";
import { type AlertsQuery, type AlertsResponse, fetchAlerts } from "../api/alerts";

interface UseAlertsResult {
  data: AlertsResponse | null;
  error: Error | null;
  loading: boolean;
}

/**
 * Polling hook for /api/alerts with filter support. Same abort-on-unmount
 * pattern as useStatus — a filter change or a component unmount cancels
 * the in-flight request instead of racing with the next one.
 *
 * Pattern: stale-while-revalidate — keeps the previous data on screen
 * while a fresh request is in flight, so filter changes don't blank the
 * table. Errors are surfaced but non-blocking.
 */
export function useAlerts(query: AlertsQuery = {}, intervalMs = 5000): UseAlertsResult {
  const [data, setData] = useState<AlertsResponse | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [loading, setLoading] = useState<boolean>(true);

  // Serialize the query to a stable key so useEffect only re-fires when
  // real filter values change, not on every render of an object literal.
  const key = JSON.stringify(query);

  useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();
    const parsed = JSON.parse(key) as AlertsQuery;

    async function tick(): Promise<void> {
      try {
        const resp = await fetchAlerts(parsed, controller.signal);
        if (cancelled) return;
        setData(resp);
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
  }, [key, intervalMs]);

  return { data, error, loading };
}
