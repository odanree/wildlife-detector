import { useEffect, useState } from "react";
import { type AlertsQuery, type AlertsResponse, fetchAlerts } from "../api/alerts";

interface UseAlertsResult {
  data: AlertsResponse | null;
  error: Error | null;
  loading: boolean;
}

/**
 * Fetches /api/alerts and re-fetches when new data lands. Same shape as
 * useUnreadAlerts: subscribes to the existing /api/alerts/events SSE
 * stream and refetches on any `counts` message (counts change → new
 * alert row landed OR one was deleted → refresh the list). Falls back
 * to setInterval polling if EventSource is unavailable.
 *
 * Pattern: strangler-fig completion — the server-side counts-poller
 * already absorbs the fixed-cadence DB read once and fans out to N
 * subscribers only on change (see _start_counts_poller in
 * src/web_service.py). The client was double-polling before; now it
 * consumes the same push signal the header badge does.
 *
 * Perf impact vs the old setInterval(tick, 5000):
 *   - Idle sessions: zero requests (was 1 request / 5s indefinitely)
 *   - Active periods: refetches only when the server observed a change
 *   - No fixed cadence in the browser → no fixed cadence in Profiler
 *     tier-3 metrics (was the 52ms poll-tick reconciliation cost)
 *
 * Stale-while-revalidate semantics preserved: previous data stays on
 * screen while a new request is in flight, filter changes don't blank
 * the table, errors are surfaced but non-blocking, abort on unmount.
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

    // Shared fetch — used by initial mount, SSE refresh trigger, and
    // fallback polling. Reuses one AbortController so a component
    // unmount cancels whichever fetch is in flight.
    async function refetch(): Promise<void> {
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

    // Initial fetch — happens regardless of SSE availability so the
    // table has data on first paint without waiting for a server event.
    void refetch();

    // Try SSE — the modern path. If EventSource is unavailable or the
    // connection outright fails, fall back to the pre-strangler-fig
    // setInterval polling so the table still updates.
    let es: EventSource | null = null;
    let fallbackHandle: number | null = null;

    function startFallbackPolling(): void {
      fallbackHandle = window.setInterval(refetch, intervalMs);
    }

    if (typeof EventSource !== "undefined") {
      es = new EventSource("/api/alerts/events");
      es.onmessage = (ev) => {
        if (cancelled) return;
        try {
          const msg = JSON.parse(ev.data) as { type?: string };
          // Refetch on any counts change. Counts change → either a new
          // alert landed or one was deleted; both are reasons to
          // refresh the list. Ignore keepalive frames (they arrive as
          // SSE comments, not `data:` lines, and don't hit onmessage
          // anyway — belt-and-suspenders).
          if (msg.type === "counts") void refetch();
        } catch {
          /* malformed frame — skip */
        }
      };
      es.onerror = () => {
        // Auto-reconnect is EventSource's job for transient errors; only
        // fall back to polling if the connection is definitively CLOSED.
        if (es && es.readyState === EventSource.CLOSED && fallbackHandle === null) {
          startFallbackPolling();
        }
      };
    } else {
      startFallbackPolling();
    }

    return () => {
      cancelled = true;
      controller.abort();
      es?.close();
      if (fallbackHandle != null) window.clearInterval(fallbackHandle);
    };
  }, [key, intervalMs]);

  return { data, error, loading };
}
