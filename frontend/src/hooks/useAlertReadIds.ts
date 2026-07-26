import { useEffect, useState } from "react";
import type { AlertRow } from "../api/alerts";
import { markAlertsSeen } from "./useUnreadAlerts";

/**
 * Per-message read receipts + click-driven header-badge watermark.
 *
 * **Row highlighting** (this hook's Set<number>):
 *   `isUnread(alert) = !readIds.has(alert.id)`
 *
 * **Header badge** (existing per-camera watermark in localStorage,
 * consumed by useUnreadAlerts): now advances +1 per click via
 * markAlertRead, not per tick via useAlertsWatermark. Visiting the
 * page no longer clears the badge; only opening specific alerts does.
 *
 * That's the whole "per-message unread" idea: the read/unread state
 * of the header badge and the row highlight are driven by the SAME
 * event (opening an alert). Visit ≠ read.
 *
 * ## First-install seed
 *
 * On the very first mount ever, seedAlertReadsOnce fires and:
 *   1. Adds every currently-loaded alert id to readIds (row highlights
 *      cold-start "everything is read" instead of flooding the
 *      operator with 200 unread historical rows).
 *   2. Stamps per-camera watermarks to current server totals via
 *      /api/alerts/counts so the header badge starts at 0 instead
 *      of showing thousands of unread.
 *
 * After the seed, only markAlertRead advances anything. New alerts
 * arrive → server total increases → badge count = total - watermark
 * = number of new alerts since last click.
 *
 * ## Patterns
 * - **Read-receipt set + bounded LRU** — Set<number> capped at CAP,
 *   drop-lowest-by-id eviction. Bound by count so bursts don't push
 *   out recently-opened ones.
 * - **Event-driven pub-sub via origin-scoped BroadcastChannel** —
 *   same primitive as the watermark sync in useUnreadAlerts. Sender
 *   is module-scope, subscribers are per-hook.
 * - **Storage-event fallback** for browsers without BroadcastChannel.
 * - **Cross-hook coupling via shared markAlertsSeen** — markAlertRead
 *   reuses useUnreadAlerts' markAlertsSeen to keep the badge in
 *   sync. Alternative was duplicating the write+broadcast plumbing,
 *   which would drift.
 */

const READ_IDS_KEY = "alertReadIds";
const READ_SEEDED_KEY = "alertReadIdsSeeded";
const READ_CHANNEL_NAME = "wildlife-detector-alerts-read";
const CAP = 5000;

let _channelSender: BroadcastChannel | null = null;
function getSender(): BroadcastChannel | null {
  if (typeof BroadcastChannel === "undefined") return null;
  if (_channelSender === null) _channelSender = new BroadcastChannel(READ_CHANNEL_NAME);
  return _channelSender;
}

function readStored(): number[] {
  try {
    const raw = localStorage.getItem(READ_IDS_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter((x): x is number => typeof x === "number");
  } catch {
    return [];
  }
}

function writeStored(ids: number[]): void {
  try {
    localStorage.setItem(READ_IDS_KEY, JSON.stringify(ids));
  } catch {
    /* quota / privacy mode — accept the loss */
  }
}

/**
 * Per-camera watermark for the header badge. We advance it +1 per
 * click via markAlertRead. Read via localStorage in useUnreadAlerts.
 * Key format matches the existing watermark keys so useUnreadAlerts
 * needs no changes.
 */
function seenKey(camera: string | null): string {
  return `alertsLastSeenTotal:${camera || "all"}`;
}

/**
 * Increment a camera's watermark by +1, then also increment the
 * cross-camera "all" watermark. Emits BroadcastChannel messages via
 * markAlertsSeen so subscribers in the header (or other tabs) update
 * without waiting for a tick.
 */
function advanceWatermarkForClick(cameraId: string | null | undefined, alertId: number): void {
  const stamp = (cam: string | null) => {
    try {
      const cur = Number(localStorage.getItem(seenKey(cam)) || "0");
      markAlertsSeen(cam, cur + 1, alertId);
    } catch {
      /* ignore */
    }
  };
  if (cameraId) stamp(cameraId);
  stamp(null); // also the "all" scope so the un-scoped header updates
}

/**
 * One-time bulk-mark on first-ever install. Seeds:
 *   - readIds set with currently-loaded alert ids (no unread flood)
 *   - per-camera watermarks with current server totals (badge at 0)
 *
 * Idempotent — guarded by a localStorage flag so subsequent visits
 * pass through untouched. The counts fetch runs fire-and-forget; if
 * it fails, next visit will re-seed the flag (the flag is set only
 * on success). Non-blocking for the caller.
 */
export function seedAlertReadsOnce(alerts: readonly AlertRow[]): void {
  try {
    if (localStorage.getItem(READ_SEEDED_KEY)) return;
    if (alerts.length === 0) return;

    // 1) Seed the readIds set with everything currently loaded.
    const cur = new Set(readStored());
    for (const a of alerts) cur.add(a.id);
    const sorted = [...cur].sort((a, b) => a - b);
    const capped = sorted.length > CAP ? sorted.slice(sorted.length - CAP) : sorted;
    writeStored(capped);

    // 2) Fetch server counts and stamp per-camera watermarks so the
    //    header badge starts at 0. Fire-and-forget — the flag is set
    //    inside the async block only after both writes succeed, so a
    //    fetch failure means we retry on the next mount.
    const perCamMaxId: Record<string, number> = {};
    for (const a of alerts) {
      if (!a.camera_id) continue;
      const prev = perCamMaxId[a.camera_id] ?? 0;
      if (a.id > prev) perCamMaxId[a.camera_id] = a.id;
    }
    const overallMax = alerts.reduce((m, a) => Math.max(m, a.id), 0);

    (async () => {
      try {
        const r = await fetch("/api/alerts/counts");
        if (!r.ok) return;
        const counts = (await r.json()) as Record<string, number>;
        for (const [cam, total] of Object.entries(counts)) {
          markAlertsSeen(cam, total, perCamMaxId[cam]);
        }
        const overallTotal = Object.values(counts).reduce((s, n) => s + n, 0);
        markAlertsSeen(null, overallTotal, overallMax);
        localStorage.setItem(READ_SEEDED_KEY, "1");
        getSender()?.postMessage({ type: "invalidate" });
      } catch {
        /* transient — next visit retries */
      }
    })();
  } catch {
    /* quota / privacy mode — next visit will retry */
  }
}

/**
 * Mark an alert as read. Idempotent — a repeat call on an already-
 * known id is a cheap no-op (no write, no broadcast).
 *
 * Side effect: also advances the per-camera watermark by +1 (and the
 * cross-camera "all" watermark), which is what the header badge in
 * useUnreadAlerts consumes. So opening an alert clears one from the
 * badge count AND drops the row highlight in one call.
 */
export function markAlertRead(alert: AlertRow): void {
  const cur = readStored();
  const set = new Set(cur);
  if (set.has(alert.id)) return;
  set.add(alert.id);
  const sorted = [...set].sort((a, b) => a - b);
  const capped = sorted.length > CAP ? sorted.slice(sorted.length - CAP) : sorted;
  writeStored(capped);
  getSender()?.postMessage({ type: "invalidate" });
  advanceWatermarkForClick(alert.camera_id, alert.id);
}

/**
 * Subscribe to the read-ids set. Returns a fresh Set on every
 * cross-tab or same-tab update so `.has(id)` reads stay live.
 */
export function useAlertReadIds(): Set<number> {
  const [ids, setIds] = useState<Set<number>>(() => new Set(readStored()));

  useEffect(() => {
    const reread = () => setIds(new Set(readStored()));

    let sub: BroadcastChannel | null = null;
    let onStorage: ((e: StorageEvent) => void) | null = null;

    if (typeof BroadcastChannel !== "undefined") {
      sub = new BroadcastChannel(READ_CHANNEL_NAME);
      sub.onmessage = reread;
    } else {
      onStorage = (e: StorageEvent) => {
        if (e.key === READ_IDS_KEY) reread();
      };
      window.addEventListener("storage", onStorage);
    }

    return () => {
      sub?.close();
      if (onStorage) window.removeEventListener("storage", onStorage);
    };
  }, []);

  return ids;
}
