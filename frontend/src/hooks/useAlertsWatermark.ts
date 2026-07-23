import { useEffect, useRef, useState } from "react";
import type { AlertRow } from "../api/alerts";
import { markAlertsSeen, readLastSeenId } from "./useUnreadAlerts";

/**
 * Watermark management for the alerts page — extracted from AlertsPage
 * during the god-component refactor (#33). Owns two related concerns:
 *
 * 1. **initialSeenId snapshot** — frozen at mount (and on camera-filter
 *    change) so unread-row highlighting stays consistent as new alerts
 *    arrive. Without the snapshot the highlight would flicker away the
 *    moment the polling tick wrote a fresh watermark.
 *
 * 2. **Alerts-seen ledger writes** — being on this page IS the "seen"
 *    event. On every polling tick we stamp the current camera's
 *    watermark (cheap local write). Once per unfiltered-view session
 *    we also fetch `/api/alerts/counts` and stamp every camera's
 *    watermark so the dual-pane header badge clears too.
 *
 * Patterns:
 * - **Adjust-state-during-rendering** for the camera-change re-snapshot
 *   (React docs' "You Might Not Need an Effect"). No subscribing
 *   effect, no dual source of truth. See #37.
 * - **Request-idempotency-via-ref** for the once-per-session counts
 *   fetch. Prior version fired on every polling tick → wasted HTTP
 *   round-trip per active operator. Fixed in #37.
 * - **AbortController** on the fetch — cancel in flight if the
 *   effect re-runs before the response lands.
 */

export interface AlertsWatermarkApi {
  /** Frozen watermark at page mount (or camera-filter change) — for
   *  row-highlighting only. Rows with `alert.id > initialSeenId` are
   *  the "unread since you last looked" set. */
  initialSeenId: number | null;
}

interface UseAlertsWatermarkOpts {
  /** Whatever `useAlerts` returned this render. When null we haven't
   *  fetched yet — no writes fire. */
  data: { total: number; items: AlertRow[] } | null;
  /** Current camera filter — "" for unfiltered / cross-camera view. */
  camera: string;
}

export function useAlertsWatermark({ data, camera }: UseAlertsWatermarkOpts): AlertsWatermarkApi {
  const [initialSeenId, setInitialSeenId] = useState<number | null>(() =>
    readLastSeenId(camera || null),
  );

  // Adjust-state-during-rendering: re-snapshot on camera-filter change
  // without a subscribing effect. React sees the setState during render,
  // bails out, re-renders with the fresh initialSeenId.
  const [prevCameraForSeen, setPrevCameraForSeen] = useState<string>(camera);
  if (camera !== prevCameraForSeen) {
    setPrevCameraForSeen(camera);
    setInitialSeenId(readLastSeenId(camera || null));
  }

  // Alerts-seen ledger — cheap local writes every tick, expensive
  // /api/alerts/counts fetch once per filter-state change. Guard with
  // a ref keyed on the filter (camera swap invalidates the guard
  // via the reset effect below).
  const countsFetchedForRef = useRef<string | null>(null);
  const items = data?.items ?? [];
  useEffect(() => {
    if (!data) return;
    const overallMaxId = items.reduce((m, a) => Math.max(m, a.id), 0);
    if (initialSeenId === null) setInitialSeenId(overallMaxId);

    if (camera) {
      // Filtered view — stamp just this camera. Runs every tick so
      // newly-arrived alerts get stamped as seen too.
      markAlertsSeen(camera, data.total, overallMaxId);
      return;
    }

    // Unfiltered — cheap local writes every tick, expensive counts
    // fetch once per filter-state.
    markAlertsSeen(null, data.total, overallMaxId);
    const key = "unfiltered";
    if (countsFetchedForRef.current === key) return;
    countsFetchedForRef.current = key;
    const controller = new AbortController();
    (async () => {
      try {
        const r = await fetch("/api/alerts/counts", { signal: controller.signal });
        if (!r.ok) return;
        const counts = (await r.json()) as Record<string, number>;
        const perCamMaxId: Record<string, number> = {};
        for (const a of items) {
          if (!a.camera_id) continue;
          const prev = perCamMaxId[a.camera_id] ?? 0;
          if (a.id > prev) perCamMaxId[a.camera_id] = a.id;
        }
        for (const [cam, total] of Object.entries(counts)) {
          markAlertsSeen(cam, total, perCamMaxId[cam]);
        }
      } catch (e) {
        if (e instanceof DOMException && e.name === "AbortError") return;
        // Silent — filtered-view watermark already stamped above,
        // dual-pane badge just won't clear this cycle.
      }
    })();
    return () => {
      controller.abort();
    };
    // camera IS in deps: switching from filtered → unfiltered must let
    // the guard-check re-run with a fresh key. We compare inside via
    // countsFetchedForRef, so no double-fetch.
  }, [data, items, initialSeenId, camera]);

  // Invalidate the counts-fetched guard when camera filter changes so
  // a camera-swap → back-to-unfiltered re-runs the counts fetch once.
  // biome-ignore lint/correctness/useExhaustiveDependencies: `camera` is the intentional trigger — body doesn't read it, just resets the guard on change.
  useEffect(() => {
    countsFetchedForRef.current = null;
  }, [camera]);

  return { initialSeenId };
}
