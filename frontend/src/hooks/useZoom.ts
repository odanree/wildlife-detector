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

      // Read the pre-zoom scroll baseline so we can compute the new
      // absolute target (setting scrollLeft = ... instead of +=). The
      // += form fights the browser's own scroll-preservation logic
      // when content resizes; absolute assignment doesn't.
      const scrollHost = document.getElementById("live-scroll-host");
      const oldScrollLeft = scrollHost?.scrollLeft ?? 0;

      // Deterministic new rendered size from base dims × new zoom.
      // getBoundingClientRect() post-commit races React's paint
      // pipeline; math from base dims doesn't.
      const oldRenderedW = baseW * oldZoom;
      const newRenderedW = baseW * newZoom;
      const newRenderedH = baseH * newZoom;
      const oldRenderedH = baseH * oldZoom;
      const dH = newRenderedH - oldRenderedH;
      // Target scroll: preserve the image-space X under the cursor.
      // cursor_screen_x = image_left + fracX * imageW - scrollLeft
      // Solve for new scrollLeft so cursor_screen_x is unchanged:
      const targetScrollLeft = oldScrollLeft + fracX * (newRenderedW - oldRenderedW);

      // flushSync commits React state synchronously so the img's
      // width attribute lands in the DOM before we touch scroll.
      flushSync(() => {
        setZoomTo(newZoom);
      });

      if (scrollHost) {
        // Force layout by reading a size property AFTER the width
        // attribute lands but BEFORE we set scrollLeft. Without this
        // the browser may not have expanded the scroll range yet and
        // our scrollLeft assignment gets clamped to the pre-zoom max.
        void scrollHost.scrollWidth;
        scrollHost.scrollLeft = targetScrollLeft;
      }
      window.scrollBy(0, dH * fracY);
    },
    [setZoomTo, min, max, step, baseW, baseH],
  );

  return { zoom, adjustBy, setZoomTo, onWheel };
}

function clamp(n: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, n));
}
