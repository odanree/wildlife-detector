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
  /** Ref to the wrapper element that carries the size + transform.
   * Children (img, overlay SVG) sit inside at 100% width/height, so
   * they scale + translate together with the wrapper via CSS. */
  canvasRef: MutableRefObject<HTMLElement | null>;
}

/**
 * Cursor-anchored zoom hook. Rewritten in PR 12b to publish state as
 * CSS variables on a single wrapper element:
 *   --rendered-w, --rendered-h  → wrapper size (baseW/H × zoom)
 *   --pan-x,      --pan-y       → translate offset in CSS pixels
 *
 * Every child inside the wrapper (stream img, zone-editor SVG
 * overlay, mask-editor overlay later) sits at 100% width/height and
 * gets the transform for free. No per-child style mutation, no
 * imperative width/height writes, no ref-per-child.
 *
 * Anchor math: pan increases by fracPos × sizeDelta so the image
 * pixel under the cursor stays put across zoom steps.
 *
 * Pattern: shared CSS-variable state as a design-system escape hatch
 * for cross-cutting values. Same discipline as the color/spacing
 * tokens in index.css — the wrapper element is the canonical carrier
 * for zoom/pan; components subscribe by living inside it.
 */
export function useZoom(cameraId: string, options: UseZoomOptions) {
  const { storageKey, min = 0.5, max = 3.0, step = 0.1, baseW, baseH, canvasRef } = options;
  const key = `${storageKey}:${cameraId || "default"}`;

  const [zoom, setZoom] = useState<number>(() => {
    const saved = localStorage.getItem(key);
    return saved ? clamp(Number.parseFloat(saved), min, max) : 1.0;
  });

  const zoomRef = useRef(zoom);
  zoomRef.current = zoom;

  const panXRef = useRef(0);
  const panYRef = useRef(0);

  useEffect(() => {
    const saved = localStorage.getItem(key);
    setZoom(saved ? clamp(Number.parseFloat(saved), min, max) : 1.0);
    panXRef.current = 0;
    panYRef.current = 0;
  }, [key, min, max]);

  const publish = useCallback(
    (z: number, panX: number, panY: number) => {
      const el = canvasRef.current;
      if (!el) return;
      el.style.setProperty("--rendered-w", `${baseW * z}px`);
      el.style.setProperty("--rendered-h", `${baseH * z}px`);
      el.style.setProperty("--pan-x", `${panX}px`);
      el.style.setProperty("--pan-y", `${panY}px`);
    },
    [baseW, baseH, canvasRef],
  );

  useLayoutEffect(() => {
    publish(zoom, panXRef.current, panYRef.current);
  }, [zoom, publish]);

  const setZoomTo = useCallback(
    (value: number) => {
      const clamped = clamp(Math.round(value * 100) / 100, min, max);
      // Reset pan when zooming via button — no cursor position to anchor from.
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
    (e: ReactWheelEvent<HTMLElement>) => {
      e.preventDefault();
      const el = canvasRef.current;
      if (!el) return;
      const oldZoom = zoomRef.current;
      const newZoom = clamp(
        Math.round((oldZoom + (e.deltaY < 0 ? step : -step)) * 100) / 100,
        min,
        max,
      );
      if (newZoom === oldZoom) return;

      const rect = el.getBoundingClientRect();
      const fracX = rect.width > 0 ? (e.clientX - rect.left) / rect.width : 0.5;
      const fracY = rect.height > 0 ? (e.clientY - rect.top) / rect.height : 0.5;

      const oldW = rect.width;
      const oldH = rect.height;
      const newW = baseW * newZoom;
      const newH = baseH * newZoom;

      panXRef.current += fracX * (newW - oldW);
      panYRef.current += fracY * (newH - oldH);

      publish(newZoom, panXRef.current, panYRef.current);

      setZoom(newZoom);
      zoomRef.current = newZoom;
      localStorage.setItem(key, String(newZoom));
    },
    [publish, min, max, step, baseW, baseH, key, canvasRef],
  );

  return { zoom, adjustBy, setZoomTo, onWheel };
}

function clamp(n: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, n));
}
