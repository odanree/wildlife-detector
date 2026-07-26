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
 * Composite with the watermark: a row is "unread" iff
 *   `alert.id > initialSeenId && !readIds.has(alert.id)`.
 * The watermark is a per-visit macro clear ("everything up to my
 * last visit"); per-message state is the micro override ("but I
 * specifically opened these").
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
