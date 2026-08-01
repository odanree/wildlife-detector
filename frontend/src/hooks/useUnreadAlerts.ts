import { useEffect, useMemo, useState } from "react";

/** Base key; camera scope (or "all") is suffixed onto it so per-camera
 *  watermarks don't leak across cameras. */
const SEEN_KEY_BASE = "alertsLastSeenTotal";
const SEEN_ID_KEY_BASE = "alertsLastSeenId";

/** BroadcastChannel name — single pub-sub primitive for watermark
 *  updates, same-tab AND cross-tab. Replaces the earlier belt-and-
 *  suspenders combo of `storage` event (cross-tab only, native
 *  browser) + custom `alerts-seen-updated` event (same-tab only,
 *  our own workaround). BroadcastChannel is the standard API for
 *  this — cleaner, single mechanism.
 *
 *  Semantics: BroadcastChannel messages are delivered to every
 *  BroadcastChannel INSTANCE on the same origin subscribed to the
 *  same name, EXCEPT the instance that called postMessage(). So we
 *  use a module-scope sender + per-hook subscriber; sender posts,
 *  every subscriber (including in the same tab) receives. */
const SEEN_CHANNEL_NAME = "wildlife-detector-alerts-seen";
interface SeenMessage {
  camera: string; // "all" or camera_id — the localStorage key suffix
  total: number;
}

/** Sender-side channel. Module-scope so we don't rebuild it on every
 *  markAlertsSeen call. Guarded because SSR / very old browsers don't
 *  have BroadcastChannel. */
let _seenSender: BroadcastChannel | null = null;
function getSeenSender(): BroadcastChannel | null {
  if (typeof BroadcastChannel === "undefined") return null;
  if (_seenSender === null) _seenSender = new BroadcastChannel(SEEN_CHANNEL_NAME);
  return _seenSender;
}

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
 * Poll alert counts for one OR MORE cameras and sum unread across all
 * of them. The header badge in dual-pane view uses this to cover both
 * visible cameras so activity on either shows up in the count
 * regardless of which is primary.
 *
 * Single camera: fetches /api/alerts?limit=1&camera=<id>.
 * Multi-camera: fetches /api/alerts/counts once — batch response with
 * per-camera totals in one request. Sums unread per camera against
 * its per-camera watermark.
 *
 * Watermarks are per-camera: visiting /alerts?camera=yard clears
 * yard's unread contribution but leaves rooftop's alone.
 * Cold-start-per-camera prevents "99+" for historical alerts.
 *
 * Pattern: monotonic-counter diff with union-scoped aggregation.
 * The scope is a SET of cameras (visible in the panes), not a single
 * entity — badge = Σ per-camera unread.
 */
export function useUnreadAlerts(
  cameras?: readonly string[] | null,
  intervalMs = 5000,
): { unread: number; unlabeled: number } {
  // Normalize: empty / undefined → cross-camera pseudo-scope "all".
  const camsKey = cameras && cameras.length > 0 ? cameras.join(",") : "all";
  const cams = useMemo(() => camsKey.split(","), [camsKey]);

  const [totals, setTotals] = useState<Record<string, number>>({});
  const [unlabeledCounts, setUnlabeledCounts] = useState<Record<string, number>>({});
  const [seens, setSeens] = useState<Record<string, number | null>>(() => readWatermarks(cams));

  // Adjust-state-during-rendering for the cams-change reset. Was a
  // useEffect([camsKey]) that called three setStates in sequence —
  // React docs call this the "no-adjust-state-when-a-prop-changes"
  // anti-pattern because it delays the reset by a render (stale
  // frame flashes) and inflates the anti-pattern-delta CI count.
  //
  // The `prevCamsKey` sentinel lets us detect the change during render:
  // React sees the setStates, discards the current render output, and
  // re-runs with the reset applied. No stale frame, no useEffect.
  //
  // Pattern: **React derived-state via prev-prop sentinel** — same
  // shape as PR #43's zone/mask reset refactor.
  const [prevCamsKey, setPrevCamsKey] = useState<string>(camsKey);
  if (camsKey !== prevCamsKey) {
    setPrevCamsKey(camsKey);
    setSeens(readWatermarks(cams));
    setTotals({});
    setUnlabeledCounts({});
  }

  // Server-pushed counts via SSE — one persistent EventSource connection
  // per tab. Replaces the previous per-tab setInterval that hit
  // /api/alerts/counts every intervalMs. Server-side polls the DB at a
  // fixed rate and fans out to all subscribers only on change, so DB
  // load is O(1) regardless of connected tabs and no HTTP requests
  // clutter DevTools per tick.
  //
  // The `all`-only single-camera case is a UI convention (no explicit
  // camera list → cross-camera pseudo-scope). SSE always returns
  // per-camera counts; we sum manually for the "all" case below.
  //
  // Cross-tab watermark sync stays on the `storage` event — that's a
  // browser-native pub-sub across same-origin tabs; nothing to do with
  // SSE.
  // biome-ignore lint/correctness/useExhaustiveDependencies: cams identity tracked via camsKey
  useEffect(() => {
    let cancelled = false;
    // AbortController only used for the fallback fetch path if SSE fails.
    const controller = new AbortController();

    // Cold-start helper — mutates seens map in place if the given
    // counts snapshot fills in a null watermark for a camera we care
    // about. Called from both the SSE onmessage handler and the
    // fetch-fallback handler so the logic doesn't duplicate.
    function coldStartSeens(newTotals: Record<string, number>): void {
      setSeens((prev) => {
        const next: Record<string, number | null> = { ...prev };
        let mutated = false;
        for (const c of cams) {
          if (next[c] === null && newTotals[c] != null) {
            try {
              localStorage.setItem(seenKey(c), String(newTotals[c]));
            } catch {
              /* ignore */
            }
            next[c] = newTotals[c];
            mutated = true;
          }
        }
        return mutated ? next : prev;
      });
    }

    // Normalize server-side counts (all cameras) to the shape this
    // hook publishes (subset filtered to `cams`, "all" as sum).
    function projectCounts(serverCounts: Record<string, number>): Record<string, number> {
      if (cams.length === 1 && cams[0] === "all") {
        return { all: Object.values(serverCounts).reduce((s, n) => s + n, 0) };
      }
      const out: Record<string, number> = {};
      for (const c of cams) out[c] = serverCounts[c] ?? 0;
      return out;
    }

    // Mirror the raw server counts into localStorage so module-level
    // helpers (advanceWatermarkForClick in useAlertReadIds) can read
    // server truth without needing a shared React state channel. This
    // is the load-bearing input for the watermark-clamp fix — without
    // it, per-click watermark increments could drift above server_total
    // and pin the unread badge to 0 forever. Written on every SSE
    // frame; last-write-wins across multiple useUnreadAlerts consumers
    // is fine since they receive identical frames.
    function mirrorServerTotals(serverCounts: Record<string, number>): void {
      try {
        for (const [cam, n] of Object.entries(serverCounts)) {
          localStorage.setItem(`alertsServerTotal:${cam}`, String(n));
        }
        const all = Object.values(serverCounts).reduce((s, n) => s + n, 0);
        localStorage.setItem("alertsServerTotal:all", String(all));
      } catch {
        /* quota / privacy mode — advance-clamp will fall back to fail-open */
      }
    }

    // Try SSE first — the modern path. If EventSource is unavailable or
    // errors, fall back to one-shot fetch per intervalMs (the pre-SSE
    // behavior) so the badge still updates.
    let es: EventSource | null = null;
    let fallbackHandle: number | null = null;

    function startFallbackPolling(): void {
      async function tick(): Promise<void> {
        try {
          const raw = await fetchCountsBreakdown(controller.signal);
          if (cancelled) return;
          // Project totals in the scope shape; keep RAW for the mirror
          // (mirror keys off camera_id, not scope).
          const projectedTotals = projectCounts(raw.total);
          setTotals(projectedTotals);
          coldStartSeens(projectedTotals);
          mirrorServerTotals(raw.total);
          setUnlabeledCounts(projectCounts(raw.unlabeled));
        } catch (e) {
          if (cancelled) return;
          if (e instanceof DOMException && e.name === "AbortError") return;
          // Silent — header badge failure isn't worth surfacing.
        }
      }
      void tick();
      fallbackHandle = window.setInterval(tick, intervalMs);
    }

    if (typeof EventSource !== "undefined") {
      es = new EventSource("/api/alerts/events");
      es.onmessage = (ev) => {
        if (cancelled) return;
        try {
          const msg = JSON.parse(ev.data) as {
            type?: string;
            counts?: Record<string, number>;
            unlabeled?: Record<string, number>;
          };
          if (msg.type !== "counts" || !msg.counts) return;
          // Mirror RAW server counts (all cameras) before projection so
          // the localStorage mirror is complete regardless of which hook
          // instance's scope is projecting the data.
          mirrorServerTotals(msg.counts);
          const projected = projectCounts(msg.counts);
          setTotals(projected);
          coldStartSeens(projected);
          // Unlabeled counts — same projection shape as totals so the
          // "all" scope sums correctly and per-camera scopes see only
          // their cameras.
          if (msg.unlabeled) {
            setUnlabeledCounts(projectCounts(msg.unlabeled));
          }
        } catch {
          /* malformed frame — skip */
        }
      };
      es.onerror = () => {
        // Auto-reconnect is EventSource's job for transient errors; only
        // fall back to polling if the connection outright fails (state
        // stays CLOSED). We check on next tick.
        if (es && es.readyState === EventSource.CLOSED && fallbackHandle === null) {
          startFallbackPolling();
        }
      };
    } else {
      startFallbackPolling();
    }

    // Watermark sync via BroadcastChannel — one primitive handles
    // both same-tab AND cross-tab updates. Every markAlertsSeen call
    // posts to the sender channel; every subscriber (including THIS
    // hook, since we're a different BroadcastChannel instance from
    // the sender) receives.
    //
    // Fallback for browsers with no BroadcastChannel: keep the legacy
    // `storage` event listener so cross-tab sync still works even if
    // same-tab won't. Modern browsers (all evergreen 2022+) support
    // BroadcastChannel natively.
    const watchedCameras = new Set(cams);
    let subscriberChannel: BroadcastChannel | null = null;
    let onStorageFallback: ((e: StorageEvent) => void) | null = null;

    if (typeof BroadcastChannel !== "undefined") {
      subscriberChannel = new BroadcastChannel(SEEN_CHANNEL_NAME);
      subscriberChannel.onmessage = (e: MessageEvent<SeenMessage>) => {
        const msg = e.data;
        if (!msg || !watchedCameras.has(msg.camera)) return;
        setSeens((prev) => ({ ...prev, [msg.camera]: msg.total }));
      };
    } else {
      const watchedKeys = new Set(cams.map((c) => seenKey(c)));
      onStorageFallback = (e: StorageEvent) => {
        if (!e.key || !watchedKeys.has(e.key) || e.newValue === null) return;
        const camera = e.key.slice(SEEN_KEY_BASE.length + 1);
        setSeens((prev) => ({
          ...prev,
          [camera]: Number.parseInt(e.newValue as string, 10) || 0,
        }));
      };
      window.addEventListener("storage", onStorageFallback);
    }

    return () => {
      cancelled = true;
      controller.abort();
      es?.close();
      if (fallbackHandle != null) window.clearInterval(fallbackHandle);
      subscriberChannel?.close();
      if (onStorageFallback) window.removeEventListener("storage", onStorageFallback);
    };
  }, [camsKey, intervalMs]);

  const unread = cams.reduce((sum, c) => {
    const s = seens[c];
    const t = totals[c] ?? 0;
    return sum + (s === null ? 0 : Math.max(0, t - s));
  }, 0);

  const unlabeled = cams.reduce((sum, c) => sum + (unlabeledCounts[c] ?? 0), 0);

  return { unread, unlabeled };
}

function readWatermarks(cams: readonly string[]): Record<string, number | null> {
  const out: Record<string, number | null> = {};
  for (const c of cams) {
    try {
      const raw = localStorage.getItem(seenKey(c));
      out[c] = raw === null ? null : Number.parseInt(raw, 10) || 0;
    } catch {
      out[c] = null;
    }
  }
  return out;
}

/** Fetch total + unlabeled counts in one call — used by the SSE-fallback
 *  polling path so we don't need two REST endpoints when SSE isn't
 *  available. Returns raw server counts (all cameras), caller projects. */
async function fetchCountsBreakdown(
  signal: AbortSignal,
): Promise<{ total: Record<string, number>; unlabeled: Record<string, number> }> {
  const r = await fetch("/api/alerts/counts?breakdown=1", { signal });
  if (!r.ok) throw new Error(`/api/alerts/counts?breakdown=1 ${r.status}`);
  const body = (await r.json()) as {
    total?: Record<string, number>;
    unlabeled?: Record<string, number>;
  };
  return { total: body.total ?? {}, unlabeled: body.unlabeled ?? {} };
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
    // Single-primitive notification: BroadcastChannel delivers to every
    // subscribed instance on this origin except the sender, which covers
    // both same-tab (other hook instances in this tab) and cross-tab
    // (siblings on other pages) — replacing the older combo of native
    // `storage` (cross-tab-only) + custom `alerts-seen-updated` event
    // (same-tab-only). Sender is module-scope so we don't rebuild it
    // on every stamp.
    const msg: SeenMessage = { camera: camera || "all", total };
    getSeenSender()?.postMessage(msg);
  } catch {
    /* ignore */
  }
}
