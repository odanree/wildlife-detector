import { useEffect } from "react";

/** Module-level cache of detection sizes by camera id. Populated the first
 *  time we see a status snapshot for a camera; consulted on every mount so
 *  a promote-swap or secondary-pane open shows the correct aspect ratio
 *  immediately without waiting for the new /status poll to resolve. */
const detectionSizeCache = new Map<string, [number, number]>();

const FALLBACK: [number, number] = [1280, 720];

/**
 * Return the detection size for a camera, preferring the cached value
 * when we've seen this camera before. Bridges the null-gap between a
 * camera change and the first live /status snapshot for the new camera.
 *
 * Fixes: on promote-swap or secondary-add, the pane's `<canvas>` would
 * briefly render at the 1280×720 fallback aspect until the new fetch
 * resolved, causing a visible resize flash. With the cache, any camera
 * we've seen once in this session shows its true aspect immediately.
 *
 * Pattern: state ownership by natural entity + memoization at the
 * appropriate scope. Detection size belongs to the camera (a stable
 * property of its detector configuration), not to the pane holding
 * it. Module-scope Map is the right cache because it survives
 * unmount/remount but not page reload — matches the natural lifetime.
 *
 * Note on the missing setTick: an earlier version kept a
 * `useState(0)` + `setTick(n+1)` after every cache write to force a
 * re-render. That was dead — nothing about the CURRENT instance's
 * next-render output changes when we write to the cache (this call
 * already returned [liveW, liveH] on the same render; the cache is
 * for FUTURE mounts of the same camera). Only OTHER hook instances
 * would benefit from cache-change reactivity, and `setTick` didn't
 * reach them. If cross-instance invalidation ever becomes needed,
 * switch to `useSyncExternalStore` with `detectionSizeCache` as the
 * external store. See issue #35.
 */
export function useDetectionSize(
  camera: string,
  liveDims: readonly [number, number] | undefined,
): [number, number] {
  const liveW = liveDims?.[0];
  const liveH = liveDims?.[1];

  // Cache write only — no re-render trigger. This instance's return
  // value is derived from props on the current render (see below); the
  // cache is a hand-off to future mounts of the same camera.
  useEffect(() => {
    if (!camera || !liveW || !liveH) return;
    const prev = detectionSizeCache.get(camera);
    if (!prev || prev[0] !== liveW || prev[1] !== liveH) {
      detectionSizeCache.set(camera, [liveW, liveH]);
    }
  }, [camera, liveW, liveH]);

  // Live wins if present, else cache, else fallback.
  if (liveW && liveH) return [liveW, liveH];
  const cached = camera ? detectionSizeCache.get(camera) : undefined;
  return cached ?? FALLBACK;
}
