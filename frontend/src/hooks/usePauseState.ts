import { useCallback, useEffect, useState } from "react";

interface PauseStateResponse {
  cameras: Record<string, boolean>;
  all_paused?: boolean;
  any_paused?: boolean;
}

export interface PauseStateApi {
  /** Per-camera paused flags. Empty object until first fetch completes. */
  cameras: Record<string, boolean>;
  /** True when EVERY camera is paused. */
  allPaused: boolean;
  /** True when at least one camera is paused. */
  anyPaused: boolean;
  /** Toggle one camera (or set explicitly when `paused` is given). */
  togglePause: (camera: string, paused?: boolean) => Promise<void>;
  /** Fan-out: pause or unpause every camera. When `paused` omitted, toggles
   *  based on current all-paused state (server-side logic). */
  toggleAll: (paused?: boolean) => Promise<void>;
  /** Force a re-fetch (rarely needed — mutations already refresh). */
  refresh: () => void;
}

/**
 * Per-camera pause hook — file-sentinel backed. Reads current state
 * once on mount + after every mutation. Sole writer for both the
 * LivePreviewPage "pause all" shortcut and the per-CameraPane toggle,
 * so the two never drift.
 *
 * Pattern name: single-writer state at the mutation boundary. Same
 * shape as the alerts label overlay — every state change goes through
 * the same setter that also fires the server POST + refetches on
 * success, so no code path can leave the local mirror stale.
 */
export function usePauseState(): PauseStateApi {
  const [cameras, setCameras] = useState<Record<string, boolean>>({});
  const [refreshTick, setRefreshTick] = useState(0);

  const refresh = useCallback(() => setRefreshTick((n) => n + 1), []);

  // refreshTick is the intentional fire trigger, effect body only
  // calls setCameras (no reads of refreshTick).
  // biome-ignore lint/correctness/useExhaustiveDependencies: refreshTick IS the fire trigger for the re-fetch
  useEffect(() => {
    let cancelled = false;
    fetch("/api/pause")
      .then((r) => r.json())
      .then((d: PauseStateResponse) => {
        if (cancelled) return;
        setCameras(d.cameras || {});
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [refreshTick]);

  const applyResponse = useCallback(
    (d: PauseStateResponse | { camera?: string; paused?: boolean }) => {
      // POST /api/pause?all=1 returns the whole { cameras: {...}, all_paused }
      if ("cameras" in d && d.cameras) {
        setCameras(d.cameras as Record<string, boolean>);
        return;
      }
      // POST /api/pause?camera=<id> returns { camera, paused }
      const single = d as { camera?: string; paused?: boolean };
      if (single.camera && typeof single.paused === "boolean") {
        setCameras((prev) => ({ ...prev, [single.camera as string]: single.paused as boolean }));
      }
    },
    [],
  );

  const togglePause = useCallback(
    async (camera: string, paused?: boolean) => {
      const params = new URLSearchParams({ camera });
      if (typeof paused === "boolean") params.set("paused", String(paused));
      const r = await fetch(`/api/pause?${params.toString()}`, { method: "POST" });
      if (r.ok) applyResponse(await r.json());
    },
    [applyResponse],
  );

  const toggleAll = useCallback(
    async (paused?: boolean) => {
      const params = new URLSearchParams({ all: "1" });
      if (typeof paused === "boolean") params.set("paused", String(paused));
      const r = await fetch(`/api/pause?${params.toString()}`, { method: "POST" });
      if (r.ok) applyResponse(await r.json());
    },
    [applyResponse],
  );

  const values = Object.values(cameras);
  const allPaused = values.length > 0 && values.every(Boolean);
  const anyPaused = values.some(Boolean);

  return { cameras, allPaused, anyPaused, togglePause, toggleAll, refresh };
}
