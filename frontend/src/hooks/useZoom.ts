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
 * Cursor-anchored zoom hook. Fourth iteration; the first three tried
 * to keep the cursor stable by adjusting scrollLeft / window.scrollY.
 * That approach fundamentally can't work when the image + surrounding
 * layout doesn't exceed viewport dimensions — no scroll room means
 * the scroll delta gets clamped to 0 and anchor breaks. For yard
 * camera at typical zoom levels the image alone doesn't exceed
 * viewport height, so vertical scroll room is always 0-ish and the
 * anchor slips.
 *
 * Current design uses CSS transform: translate on the image to shift
 * it relative to its layout box. Works regardless of scroll room.
 * Cost: the img's LAYOUT box stays at its natural position — the
 * transform is a visual-only offset. Zone-editor coord math (next
 * PR) reads getBoundingClientRect which returns the VISUAL position
 * (transform included), so coord conversion still works.
 *
 * Wheel handler:
 *   1. Read current img rect + panX/panY refs
 *   2. Compute new zoom + new pan such that the cursor stays over the
 *      same image pixel: newPan = oldPan + fracPos × sizeDelta
 *   3. Mutate img.style.width/height/transform imperatively
 *   4. Update React state for the display %
 *
 * Reset (button click, camera switch): resets pan to 0 alongside the
 * zoom change.
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

  // Pan offsets (in CSS pixels). translate() shifts image away from
  // its layout box; positive panX moves the image LEFT visually.
  const panXRef = useRef(0);
  const panYRef = useRef(0);

  useEffect(() => {
    const saved = localStorage.getItem(key);
    setZoom(saved ? clamp(Number.parseFloat(saved), min, max) : 1.0);
    panXRef.current = 0;
    panYRef.current = 0;
  }, [key, min, max]);

  const applyToImg = useCallback(
    (z: number, panX: number, panY: number) => {
      const img = imgRef.current;
      if (!img) return;
      img.style.width = `${baseW * z}px`;
      img.style.height = `${baseH * z}px`;
      img.style.transform = `translate(${-panX}px, ${-panY}px)`;
    },
    [baseW, baseH, imgRef],
  );

  // Sync img style with current zoom/pan on non-wheel changes (button
  // clicks, camera switch reset, initial mount).
  useLayoutEffect(() => {
    applyToImg(zoom, panXRef.current, panYRef.current);
  }, [zoom, applyToImg]);

  const setZoomTo = useCallback(
    (value: number) => {
      const clamped = clamp(Math.round(value * 100) / 100, min, max);
      // Reset pan when zooming via button — no cursor position to
      // anchor from.
      panXRef.current = 0;
      panYRef.current = 0;
      setZoom(clamped);
      zoomRef.current = clamped;
      localStorage.setItem(key, String(clamped));
      return clamped;
    },
    [min, max, key],
  );

  const adjustBy = useCallback((delta: number) => setZoomTo(zoomRef.current + delta), [setZoomTo]);

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

      // Old rendered size — trust the rect (accounts for any prior
      // pan/zoom drift from browser rounding).
      const oldW = rect.width;
      const oldH = rect.height;
      const newW = baseW * newZoom;
      const newH = baseH * newZoom;
      const dW = newW - oldW;
      const dH = newH - oldH;

      // Anchor math: pan increases by fracPos × sizeDelta so the
      // image pixel under the cursor stays put.
      panXRef.current += fracX * dW;
      panYRef.current += fracY * dH;

      // Imperative mutation — synchronous DOM write, browser processes
      // before returning control.
      applyToImg(newZoom, panXRef.current, panYRef.current);

      // React state catches up for the display %.
      setZoom(newZoom);
      zoomRef.current = newZoom;
      localStorage.setItem(key, String(newZoom));
    },
    [applyToImg, min, max, step, baseW, baseH, key],
  );

  return { zoom, adjustBy, setZoomTo, onWheel };
}

function clamp(n: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, n));
}
