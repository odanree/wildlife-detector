import {
  type WheelEvent as ReactWheelEvent,
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";

interface UseZoomOptions {
  /** localStorage key prefix for persisting the zoom per-camera. */
  storageKey: string;
  /** Min/max zoom factor. */
  min?: number;
  max?: number;
  /** Wheel notch step. */
  step?: number;
  /** Native image dimensions (before zoom). Used to compute deltas
   * deterministically instead of reading the DOM post-commit, which
   * races with React's async paint. */
  baseW: number;
  baseH: number;
}

/**
 * Cursor-anchored zoom hook. Returns the current zoom factor + a
 * wheel handler that adjusts scroll position after the zoom change
 * so the same image pixel stays under the cursor.
 *
 * Prior versions of this hook tried `flushSync + read scrollWidth`
 * in the wheel handler itself. The pattern was correct on paper but
 * X-axis anchor still drifted in React — the scroll write kept
 * landing before React's <img width={...}> attribute change had
 * expanded the scroll host's scroll range. Vanilla-JS equivalents
 * worked because they mutated the DOM synchronously with the wheel
 * event.
 *
 * Current design: split intent from effect.
 *   1. Wheel handler captures the desired {zoom, scrollLeft, scrollY}
 *      into a ref and calls setState.
 *   2. React commits the new zoom → <img> width attribute updates.
 *   3. useLayoutEffect fires AFTER the commit but BEFORE the browser
 *      paints. At that point the scroll host's scroll range reflects
 *      the new content size, so setting scrollLeft to the target
 *      lands correctly.
 *
 * The pattern is the React-canonical way to sync scroll position
 * with a state-driven layout change.
 */
export function useZoom(cameraId: string, options: UseZoomOptions) {
  const { storageKey, min = 0.5, max = 3.0, step = 0.1, baseW, baseH } = options;
  const key = `${storageKey}:${cameraId || "default"}`;

  const [zoom, setZoom] = useState<number>(() => {
    const saved = localStorage.getItem(key);
    return saved ? clamp(Number.parseFloat(saved), min, max) : 1.0;
  });

  const zoomRef = useRef(zoom);
  zoomRef.current = zoom;

  // Pending scroll adjustment queued by the wheel handler, applied by
  // useLayoutEffect after the zoom commits. null when nothing pending.
  const pendingScrollRef = useRef<{ scrollLeft: number; scrollDeltaY: number } | null>(null);

  useEffect(() => {
    const saved = localStorage.getItem(key);
    setZoom(saved ? clamp(Number.parseFloat(saved), min, max) : 1.0);
  }, [key, min, max]);

  const setZoomTo = useCallback(
    (value: number) => {
      const clamped = clamp(Math.round(value * 100) / 100, min, max);
      setZoom(clamped);
      zoomRef.current = clamped;
      localStorage.setItem(key, String(clamped));
      return clamped;
    },
    [min, max, key],
  );

  const adjustBy = useCallback((delta: number) => setZoomTo(zoomRef.current + delta), [setZoomTo]);

  const onWheel = useCallback(
    (e: ReactWheelEvent<HTMLElement>) => {
      e.preventDefault();
      const oldZoom = zoomRef.current;
      const newZoom = clamp(
        Math.round((oldZoom + (e.deltaY < 0 ? step : -step)) * 100) / 100,
        min,
        max,
      );
      if (newZoom === oldZoom) return;

      const rect = e.currentTarget.getBoundingClientRect();
      const fracX = rect.width > 0 ? (e.clientX - rect.left) / rect.width : 0.5;
      const fracY = rect.height > 0 ? (e.clientY - rect.top) / rect.height : 0.5;

      const scrollHost = document.getElementById("live-scroll-host");
      const oldScrollLeft = scrollHost?.scrollLeft ?? 0;

      const oldRenderedW = baseW * oldZoom;
      const newRenderedW = baseW * newZoom;
      const oldRenderedH = baseH * oldZoom;
      const newRenderedH = baseH * newZoom;

      // Absolute new scroll targets to keep cursor over the same
      // image pixel. See derivation in commit history.
      pendingScrollRef.current = {
        scrollLeft: oldScrollLeft + fracX * (newRenderedW - oldRenderedW),
        scrollDeltaY: fracY * (newRenderedH - oldRenderedH),
      };

      // Trigger the zoom state change. useLayoutEffect below picks up
      // the pendingScrollRef after React commits and the DOM reflects
      // the new img width/height.
      setZoomTo(newZoom);
    },
    [setZoomTo, min, max, step, baseW, baseH],
  );

  // Apply queued scroll adjustment AFTER React commits the zoom into
  // the <img width/height> attributes but BEFORE the browser paints.
  // useLayoutEffect is the only hook with that timing guarantee; a
  // regular useEffect can paint an intermediate frame with the new
  // zoom but old scroll → visible flash of drift.
  //
  // `zoom` is the intended dependency — this effect must re-fire on
  // every zoom change to apply the pending scroll queued by the
  // wheel handler. Biome flags it as "unused" because the effect
  // body only reads the ref; the ref is deliberate (mutable across
  // renders without triggering rerenders) but `zoom` is the trigger.
  // biome-ignore lint/correctness/useExhaustiveDependencies: zoom is the intentional re-fire trigger; effect body uses the ref set alongside setZoomTo
  useLayoutEffect(() => {
    const pending = pendingScrollRef.current;
    if (!pending) return;
    pendingScrollRef.current = null;
    const scrollHost = document.getElementById("live-scroll-host");
    if (scrollHost) scrollHost.scrollLeft = pending.scrollLeft;
    if (pending.scrollDeltaY !== 0) window.scrollBy(0, pending.scrollDeltaY);
  }, [zoom]);

  return { zoom, adjustBy, setZoomTo, onWheel };
}

function clamp(n: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, n));
}
