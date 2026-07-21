import {
  type MutableRefObject,
  type WheelEvent as ReactWheelEvent,
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";

interface UseZoomOptions {
  storageKey: string;
  min?: number;
  max?: number;
  step?: number;
  baseW: number;
  baseH: number;
  /** Ref to the <img> being zoomed. */
  imgRef: MutableRefObject<HTMLImageElement | null>;
}

/**
 * Cursor-anchored zoom hook. React version of the vanilla-JS wheel
 * zoom on the live-preview page.
 *
 * Design after two failed attempts:
 *   - Attempt 1 (RAF + read post-commit rect): scroll write landed
 *     before React committed the new width; delta came out zero.
 *   - Attempt 2 (flushSync + useLayoutEffect): commit landed but the
 *     scroll write still didn't take on the X axis in Chromium.
 *     Suspect: the img `width` attribute (as opposed to CSS width)
 *     doesn't reliably expand the parent's scroll range in one tick.
 *
 * Current design mirrors the vanilla-JS impl exactly:
 *   1. Wheel handler mutates the img's inline `style.width/height`
 *      synchronously — a direct DOM write that browsers process
 *      before returning control.
 *   2. Reads the new rect via getBoundingClientRect() — forces the
 *      browser to compute layout right now, which also expands the
 *      scroll range.
 *   3. Sets scrollLeft/scrollY based on the actual new size.
 *   4. Only THEN updates React state (for the zoom % display).
 *
 * The img's size lives on `style.width/height`, not the React `width`
 * prop, so React's re-render doesn't fight the imperative write.
 * A useLayoutEffect syncs style for non-wheel triggers (button clicks,
 * localStorage restore, camera-switch reset).
 */
export function useZoom(cameraId: string, options: UseZoomOptions) {
  const { storageKey, min = 0.5, max = 3.0, step = 0.1, baseW, baseH, imgRef } = options;
  const key = `${storageKey}:${cameraId || "default"}`;

  const [zoom, setZoom] = useState<number>(() => {
    const saved = localStorage.getItem(key);
    return saved ? clamp(Number.parseFloat(saved), min, max) : 1.0;
  });

  const zoomRef = useRef(zoom);
  zoomRef.current = zoom;

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

  // Sync img.style.{width,height} with the current zoom for non-wheel
  // triggers (button click, camera switch, initial mount). The wheel
  // handler bypasses this by writing to style directly BEFORE calling
  // setZoomTo — the effect fires on the resulting rerender but is a
  // no-op because the style is already correct.
  useLayoutEffect(() => {
    if (imgRef.current) {
      imgRef.current.style.width = `${baseW * zoom}px`;
      imgRef.current.style.height = `${baseH * zoom}px`;
    }
  }, [zoom, baseW, baseH, imgRef]);

  const onWheel = useCallback(
    (e: ReactWheelEvent<HTMLImageElement>) => {
      e.preventDefault();
      const img = e.currentTarget;
      const oldZoom = zoomRef.current;
      const newZoom = clamp(
        Math.round((oldZoom + (e.deltaY < 0 ? step : -step)) * 100) / 100,
        min,
        max,
      );
      if (newZoom === oldZoom) return;

      const rect = img.getBoundingClientRect();
      const fracX = rect.width > 0 ? (e.clientX - rect.left) / rect.width : 0.5;
      const fracY = rect.height > 0 ? (e.clientY - rect.top) / rect.height : 0.5;

      const scrollHost = document.getElementById("live-scroll-host");
      const oldScrollLeft = scrollHost?.scrollLeft ?? 0;

      // Imperative DOM write — happens synchronously, browser processes
      // it before this function returns.
      img.style.width = `${baseW * newZoom}px`;
      img.style.height = `${baseH * newZoom}px`;

      // Read new rect. Not for the delta (we can compute it) but to
      // FORCE the browser to lay out the new size right now, which
      // expands the scroll host's scroll range so our scrollLeft
      // write can actually land.
      const newRect = img.getBoundingClientRect();
      const dW = newRect.width - rect.width;
      const dH = newRect.height - rect.height;

      if (scrollHost) scrollHost.scrollLeft = oldScrollLeft + fracX * dW;
      if (dH !== 0) window.scrollBy(0, fracY * dH);

      // React state catches up for the display. useLayoutEffect above
      // will fire on the re-render but is a no-op because we already
      // set the style.
      setZoomTo(newZoom);
    },
    [setZoomTo, min, max, step, baseW, baseH],
  );

  return { zoom, adjustBy, setZoomTo, onWheel };
}

function clamp(n: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, n));
}
