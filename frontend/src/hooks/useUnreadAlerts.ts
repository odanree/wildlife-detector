import { useEffect, useState } from "react";
import { fetchAlerts } from "../api/alerts";

const SEEN_KEY = "alertsLastSeenTotal";
export const SEEN_ID_KEY = "alertsLastSeenId";

/** Read the last-seen alert id from localStorage. Returns null if never
 *  written (cold-start), a number otherwise. Exported so AlertsPage can
 *  snapshot it on mount for per-row highlighting BEFORE the write side
 *  of markAlertsSeen() rolls the watermark forward. */
export function readLastSeenId(): number | null {
  try {
    const raw = localStorage.getItem(SEEN_ID_KEY);
    return raw === null ? null : Number.parseInt(raw, 10) || 0;
  } catch {
    return null;
  }
}

/**
 * Poll cross-camera alert total and compute unread count relative to
 * the last time the operator visited /alerts (stamped as `seen` in
 * localStorage by AlertsPage on mount).
 *
 * Uses /api/alerts?limit=1 so the payload is one row + the total —
 * cheap enough to poll every 5s. Total is monotonic across the DB,
 * so unread = max(0, total - seen) with no timestamp arithmetic.
 *
 * Pattern: monotonic-counter diff for unread state. Same shape as
 * inbox unread counts — the server exposes a monotonic counter, the
 * client stashes a "last-seen" watermark in localStorage.
 */
export function useUnreadAlerts(intervalMs = 5000): { unread: number; total: number } {
  const [total, setTotal] = useState<number>(0);
  const [seen, setSeen] = useState<number | null>(() => {
    try {
      const raw = localStorage.getItem(SEEN_KEY);
      // null (never visited) is distinct from 0 (visited when total was 0):
      // on first-ever load we adopt the current total instead of showing
      // "99+" for historical alerts we've never had a chance to see.
      return raw === null ? null : Number.parseInt(raw, 10) || 0;
    } catch {
      return null;
    }
  });

  useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();

    async function tick(): Promise<void> {
      try {
        const resp = await fetchAlerts({ limit: 1 }, controller.signal);
        if (cancelled) return;
        const t = resp.total ?? 0;
        setTotal(t);
        // Cold-start: no prior watermark → adopt current total as seen so
        // the badge starts at 0 for a fresh browser session and only counts
        // alerts that fire after the page opened.
        setSeen((prev) => {
          if (prev !== null) return prev;
          try {
            localStorage.setItem(SEEN_KEY, String(t));
          } catch {
            /* ignore */
          }
          return t;
        });
      } catch (e) {
        if (cancelled) return;
        if (e instanceof DOMException && e.name === "AbortError") return;
        // Silent — the header badge isn't worth surfacing errors for.
      }
    }

    void tick();
    const handle = window.setInterval(tick, intervalMs);

    // Cross-tab sync: another tab visiting /alerts writes SEEN_KEY,
    // and the storage event lets us update without polling localStorage.
    function onStorage(e: StorageEvent) {
      if (e.key === SEEN_KEY && e.newValue !== null) {
        setSeen(Number.parseInt(e.newValue, 10) || 0);
      }
    }
    window.addEventListener("storage", onStorage);

    return () => {
      cancelled = true;
      controller.abort();
      window.clearInterval(handle);
      window.removeEventListener("storage", onStorage);
    };
  }, [intervalMs]);

  return { unread: seen === null ? 0 : Math.max(0, total - seen), total };
}

/**
 * Mark alerts as seen. Two watermarks are written:
 *   - SEEN_KEY: the monotonic total, used by the header badge to compute
 *     count.
 *   - SEEN_ID_KEY: the highest alert id currently rendered, used by
 *     AlertsPage row-highlighting to distinguish unread rows.
 *
 * Call from AlertsPage when data arrives so a live new alert on the
 * alerts page doesn't grow the badge for a viewer who's already
 * looking at it, and so revisiting doesn't re-highlight everything.
 *
 * `highestId` is optional — when omitted, only the total watermark
 * moves. That's the correct behavior for pages that don't render row
 * detail (e.g. a hypothetical "clear all unread" button in the
 * header).
 */
export function markAlertsSeen(total: number, highestId?: number): void {
  try {
    localStorage.setItem(SEEN_KEY, String(total));
    if (highestId != null) {
      localStorage.setItem(SEEN_ID_KEY, String(highestId));
    }
  } catch {
    // localStorage unavailable — badge won't zero-out, minor cosmetic.
  }
}
