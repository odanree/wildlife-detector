import { type WheelEvent as ReactWheelEvent, useCallback, useEffect, useState } from "react";

interface UseZoomOptions {
  /** localStorage key for persisting the zoom per-camera. */
  storageKey: string;
  /** Min/max zoom factor. */
  min?: number;
  max?: number;
  /** Wheel notch step. */
  step?: number;
}

/**
 * Cursor-anchored zoom hook. Returns the current zoom factor + a
 * wheel handler that adjusts the wrap container's scroll after the
 * next paint so the cursor stays over the same image pixel.
 *
 * The scroll-shift math depends on the wrap element resizing to the
 * new zoom factor between the wheel event and the RAF callback — so
 * the container's width/height must actually respond to the zoom
 * (via inline width/height on the img). Transform: scale won't work
 * here because the wrap size wouldn't change and the scrollLeft/
 * scrollTop deltas would be zero.
 */
export function useZoom(cameraId: string, options: UseZoomOptions) {
  const { storageKey, min = 0.5, max = 3.0, step = 0.1 } = options;
  const key = `${storageKey}:${cameraId || "default"}`;

  const [zoom, setZoom] = useState<number>(() => {
    const saved = localStorage.getItem(key);
    return saved ? Math.max(min, Math.min(max, Number.parseFloat(saved))) : 1.0;
  });

  useEffect(() => {
    const saved = localStorage.getItem(key);
    if (saved) setZoom(Math.max(min, Math.min(max, Number.parseFloat(saved))));
    else setZoom(1.0);
  }, [key, min, max]);

  const adjustBy = useCallback(
    (delta: number): number => {
      const clamped = Math.max(min, Math.min(max, Math.round((zoom + delta) * 100) / 100));
      setZoom(clamped);
      localStorage.setItem(key, String(clamped));
      return clamped;
    },
    [zoom, min, max, key],
  );

  const onWheel = useCallback(
    (e: ReactWheelEvent<HTMLElement>) => {
      e.preventDefault();
      const rect = e.currentTarget.getBoundingClientRect();
      const fracX = rect.width > 0 ? (e.clientX - rect.left) / rect.width : 0.5;
      const fracY = rect.height > 0 ? (e.clientY - rect.top) / rect.height : 0.5;
      const oldW = rect.width;
      const oldH = rect.height;
      const el = e.currentTarget;
      const scrollHost = document.getElementById("live-scroll-host");

      adjustBy(e.deltaY < 0 ? step : -step);

      requestAnimationFrame(() => {
        const newRect = el.getBoundingClientRect();
        const dW = newRect.width - oldW;
        const dH = newRect.height - oldH;
        if (scrollHost) scrollHost.scrollLeft += dW * fracX;
        window.scrollBy(0, dH * fracY);
      });
    },
    [adjustBy, step],
  );

  return { zoom, adjustBy, onWheel };
}
