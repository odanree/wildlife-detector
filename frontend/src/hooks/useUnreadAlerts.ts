import { useEffect, useState } from "react";
import { fetchAlerts } from "../api/alerts";

/** Base key; camera scope (or "all") is suffixed onto it so per-camera
 *  watermarks don't leak across cameras. */
const SEEN_KEY_BASE = "alertsLastSeenTotal";
const SEEN_ID_KEY_BASE = "alertsLastSeenId";

function seenKey(camera?: string | null): string {
  return `${SEEN_KEY_BASE}:${camera || "all"}`;
}
function seenIdKey(camera?: string | null): string {
  return `${SEEN_ID_KEY_BASE}:${camera || "all"}`;
}

/** Read the last-seen alert id for a specific camera (or "all" if
 *  omitted). Returns null if never written. Exported so AlertsPage
 *  can snapshot at mount BEFORE markAlertsSeen rolls the watermark
 *  forward for row highlighting. */
export function readLastSeenId(camera?: string | null): number | null {
  try {
    const raw = localStorage.getItem(seenIdKey(camera));
    return raw === null ? null : Number.parseInt(raw, 10) || 0;
  } catch {
    return null;
  }
}

/**
 * Poll alert count and compute unread relative to the last time the
 * operator visited /alerts.
 *
 * Camera-scoped: pass `camera` to count only that camera's alerts —
 * critical for the header badge, otherwise a yard viewer sees the
 * badge tick up for rooftop alerts. Omit `camera` for cross-camera
 * total (used by the alerts page's "all" view).
 *
 * Watermark is scoped by the same camera key, so a yard viewer's
 * "seen" state doesn't leak into the rooftop badge.
 *
 * Pattern: monotonic-counter diff for unread state + per-entity
 * watermark scoping.
 */
export function useUnreadAlerts(
  camera?: string | null,
  intervalMs = 5000,
): { unread: number; total: number } {
  const [total, setTotal] = useState<number>(0);
  const key = seenKey(camera);
  const [seen, setSeen] = useState<number | null>(() => {
    try {
      const raw = localStorage.getItem(key);
      // null (never visited) is distinct from 0 (visited when total was 0):
      // on cold-start we adopt the current total instead of showing "99+"
      // for historical alerts we've never had a chance to see.
      return raw === null ? null : Number.parseInt(raw, 10) || 0;
    } catch {
      return null;
    }
  });
  // Camera change: reset seen from the NEW camera's watermark so we
  // don't briefly show the wrong count while polling races.
  useEffect(() => {
    try {
      const raw = localStorage.getItem(key);
      setSeen(raw === null ? null : Number.parseInt(raw, 10) || 0);
    } catch {
      setSeen(null);
    }
    setTotal(0);
  }, [key]);

  useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();

    async function tick(): Promise<void> {
      try {
        const q: { limit: number; camera?: string } = { limit: 1 };
        if (camera) q.camera = camera;
        const resp = await fetchAlerts(q, controller.signal);
        if (cancelled) return;
        const t = resp.total ?? 0;
        setTotal(t);
        // Cold-start for this camera: adopt current total as seen.
        setSeen((prev) => {
          if (prev !== null) return prev;
          try {
            localStorage.setItem(key, String(t));
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

    // Cross-tab sync: another tab visiting /alerts for this camera
    // writes the same key, and the storage event lets us update
    // without polling localStorage.
    function onStorage(e: StorageEvent) {
      if (e.key === key && e.newValue !== null) {
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
  }, [camera, intervalMs, key]);

  return { unread: seen === null ? 0 : Math.max(0, total - seen), total };
}

/**
 * Mark alerts as seen for a specific camera (or "all"). Writes both
 * watermarks:
 *   - total watermark → header badge unread count
 *   - highest-id watermark → alerts-page row highlighting
 *
 * Camera scope matches useUnreadAlerts. Call from AlertsPage after
 * data arrives with the same camera filter the page is showing.
 */
export function markAlertsSeen(
  camera: string | null | undefined,
  total: number,
  highestId?: number,
): void {
  try {
    localStorage.setItem(seenKey(camera), String(total));
    if (highestId != null) {
      localStorage.setItem(seenIdKey(camera), String(highestId));
    }
  } catch {
    // localStorage unavailable — badge won't zero-out, minor cosmetic.
  }
}
