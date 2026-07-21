import {
  type WheelEvent as ReactWheelEvent,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import { flushSync } from "react-dom";

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
 * Anchor math is done from `baseW × baseH × oldZoom → newZoom`
 * rather than from `getBoundingClientRect()`. Reading the rect
 * post-commit races with React's paint pipeline — sometimes RAF
 * fires before the new width lands in the DOM and the delta comes
 * out zero. Deterministic math bypasses the race.
 *
 * The scroll host must NOT be flex-centered — cursor anchoring
 * assumes a fixed top-left origin. See LivePreviewPage.module.css
 * for the rationale.
 */
export function useZoom(cameraId: string, options: UseZoomOptions) {
  const { storageKey, min = 0.5, max = 3.0, step = 0.1, baseW, baseH } = options;
  const key = `${storageKey}:${cameraId || "default"}`;

  const [zoom, setZoom] = useState<number>(() => {
    const saved = localStorage.getItem(key);
    return saved ? clamp(Number.parseFloat(saved), min, max) : 1.0;
  });

  // Keep the zoom in a ref so the wheel handler doesn't re-close over
  // stale state between rapid wheel notches.
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

  const onWheel = useCallback(
    (e: ReactWheelEvent<HTMLElement>) => {
      e.preventDefault();
      const oldZoom = zoomRef.current;
      const newZoom = clamp(
        Math.round((oldZoom + (e.deltaY < 0 ? step : -step)) * 100) / 100,
        min,
        max,
      );
      if (newZoom === oldZoom) return; // clamped at limit — nothing to anchor

      // Fraction of the current image under the cursor. currentTarget
      // rect is the ACTUAL rendered size, so this fraction is what
      // "the same image pixel" resolves to.
      const rect = e.currentTarget.getBoundingClientRect();
      const fracX = rect.width > 0 ? (e.clientX - rect.left) / rect.width : 0.5;
      const fracY = rect.height > 0 ? (e.clientY - rect.top) / rect.height : 0.5;

      // Deterministic new size from base dims × new zoom. Avoids
      // reading getBoundingClientRect() post-commit which races.
      const oldRenderedW = baseW * oldZoom;
      const oldRenderedH = baseH * oldZoom;
      const newRenderedW = baseW * newZoom;
      const newRenderedH = baseH * newZoom;
      const dW = newRenderedW - oldRenderedW;
      const dH = newRenderedH - oldRenderedH;

      const scrollHost = document.getElementById("live-scroll-host");

      // flushSync forces React to commit the zoom state change
      // synchronously so the DOM has the new width/height BEFORE we
      // adjust scroll. Without it, the scrollLeft change lands
      // against the OLD content size and gets clamped to the old
      // scroll range.
      flushSync(() => {
        setZoomTo(newZoom);
      });

      if (scrollHost) scrollHost.scrollLeft += dW * fracX;
      window.scrollBy(0, dH * fracY);
    },
    [setZoomTo, min, max, step, baseW, baseH],
  );

  return { zoom, adjustBy, setZoomTo, onWheel };
}

function clamp(n: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, n));
}
