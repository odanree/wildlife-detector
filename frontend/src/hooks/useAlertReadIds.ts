import { useEffect, useState } from "react";

/**
 * Per-message read receipts — complements the coarse camera-scoped
 * watermarks in useUnreadAlerts (which power the header badge) with
 * fine-grained "which specific alerts have I looked at" state.
 *
 * Storage model: a bounded Set<number> in localStorage, mirrored to
 * every tab via BroadcastChannel. Cap keeps the set from growing
 * unboundedly as alert IDs march up; oldest-by-id evicted first
 * once we exceed CAP. In practice the DB retention window is well
 * under CAP so eviction is rare.
 *
 * Read trigger convention: `markAlertRead(id)` fires from the single
 * AlertsPage `setOpenId(id)` wrapper — that means row-thumb click,
 * lightbox prev/next, AND preview-strip jumps all mark-as-read for
 * free. Null (close) is a no-op.
 *
 * Pure per-message semantics: a row is "unread" iff
 *   `!readIds.has(alert.id)`.
 * The watermark stays only as the header-badge summary signal — it
 * is intentionally NOT part of the row-highlight computation. An
 * earlier composite (`alert.id > initialSeenId && !readIds.has(id)`)
 * had the wrong semantics: the watermark advanced on every visit,
 * bulk-clearing unread rows before the operator could open them.
 * Per-message means visiting the page does not mark anything read;
 * only opening the specific alert does.
 *
 * Patterns:
 * - **Read-receipt set + bounded LRU** — Set<number> capped at CAP,
 *   drop-lowest eviction. Bound is by count, not by wall clock, so
 *   a burst of alerts doesn't push out recently-opened ones.
 * - **Event-driven pub-sub via origin-scoped BroadcastChannel** —
 *   same primitive as the watermark sync in useUnreadAlerts. Sender
 *   is module-scope, subscribers are per-hook; native semantics
 *   deliver to every subscriber except the sender instance, covering
 *   same-tab AND cross-tab in one call.
 * - **Storage-event fallback** for browsers without BroadcastChannel
 *   support, so cross-tab still works on older engines.
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
 * One-time bulk-mark on install: on the first-ever mount of the
 * per-message read system, seed the set with every currently-loaded
 * alert id so an operator new to this feature doesn't see 200
 * historical rows flagged unread on their first visit. Guarded by a
 * dedicated seeded-flag key so it fires exactly once per browser.
 * Subsequent installs (new alerts) default to unread until
 * explicitly opened — the correct per-message semantics.
 */
export function seedAlertReadsOnce(ids: readonly number[]): void {
  try {
    if (localStorage.getItem(READ_SEEDED_KEY)) return;
    if (ids.length === 0) return;
    const cur = new Set(readStored());
    for (const id of ids) cur.add(id);
    const sorted = [...cur].sort((a, b) => a - b);
    const capped = sorted.length > CAP ? sorted.slice(sorted.length - CAP) : sorted;
    writeStored(capped);
    localStorage.setItem(READ_SEEDED_KEY, "1");
    getSender()?.postMessage({ type: "invalidate" });
  } catch {
    /* quota / privacy mode — next visit will retry */
  }
}

/**
 * Mark one or more alerts as read. Idempotent — repeat calls with
 * already-known ids are cheap no-ops (no write, no broadcast).
 */
export function markAlertRead(...ids: number[]): void {
  const cur = readStored();
  const set = new Set(cur);
  let mutated = false;
  for (const id of ids) {
    if (!set.has(id)) {
      set.add(id);
      mutated = true;
    }
  }
  if (!mutated) return;
  // Bounded-LRU-by-id: keep the CAP highest ids. Alerts have
  // monotonically increasing ids so "highest id" ≈ "most recent."
  const sorted = [...set].sort((a, b) => a - b);
  const capped = sorted.length > CAP ? sorted.slice(sorted.length - CAP) : sorted;
  writeStored(capped);
  getSender()?.postMessage({ type: "invalidate" });
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
      // Fallback: browsers without BroadcastChannel still get the
      // native cross-tab storage event. Same-tab reactivity is lost
      // in this branch, but modern browsers all support the primary
      // path.
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
