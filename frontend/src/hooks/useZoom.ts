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
  /** Ref to the wrapper element that carries the transform. Must be
   *  sized by CSS (aspect-ratio + max-w/-h = container-fit); this hook
   *  only publishes `--zoom` and pan offsets. */
  canvasRef: MutableRefObject<HTMLElement | null>;
}

/**
 * Cursor-anchored zoom hook. Publishes CSS variables on a single
 * wrapper element:
 *   --zoom              → scale factor
 *   --pan-x, --pan-y    → translate offset in CSS pixels
 *
 * The wrapper's own dimensions come from CSS
 * (aspect-ratio + max-width/-height), so at zoom 1 it fits the
 * container regardless of image resolution. Zoom > 1 is a
 * `transform: scale(--zoom)` — overflow is clipped by the scrollHost
 * ancestor and revealed by panning.
 *
 * Anchor math: pan increases by fracPos × (newRenderedW - oldRenderedW)
 * so the image pixel under the cursor stays put across zoom steps.
 * Rendered dimensions come from getBoundingClientRect (post-transform)
 * so the hook is agnostic to the image aspect ratio and the container
 * layout — a change from the PR 12b version, which had baseW/baseH
 * baked in and broke when the container was smaller than baseW × baseH.
 *
 * Pattern: shared CSS-variable state as a design-system escape hatch
 * for cross-cutting values. Same discipline as the color/spacing
 * tokens in index.css.
 */
export function useZoom(cameraId: string, options: UseZoomOptions) {
  const { storageKey, min = 0.5, max = 3.0, step = 0.1, canvasRef } = options;
  const key = `${storageKey}:${cameraId || "default"}`;

  const [zoom, setZoom] = useState<number>(() => {
    const saved = localStorage.getItem(key);
    return saved ? clamp(Number.parseFloat(saved), min, max) : 1.0;
  });

  const zoomRef = useRef(zoom);
  zoomRef.current = zoom;

  const panXRef = useRef(0);
  const panYRef = useRef(0);

  // Reset zoom + pan when key changes (camera swap mid-lifetime). Skip
  // the very first run — useState initializer already read localStorage
  // at mount, and firing this effect on mount would cause an extra
  // render and clobber any zoom set between mount and first paint. Ref
  // guard is cheaper than adding a `key={cameraId}` prop on every caller.
  const didMountRef = useRef(false);
  useEffect(() => {
    if (!didMountRef.current) {
      didMountRef.current = true;
      return;
    }
    const saved = localStorage.getItem(key);
    setZoom(saved ? clamp(Number.parseFloat(saved), min, max) : 1.0);
    panXRef.current = 0;
    panYRef.current = 0;
  }, [key, min, max]);

  const publish = useCallback(
    (z: number, panX: number, panY: number) => {
      const el = canvasRef.current;
      if (!el) return;
      el.style.setProperty("--zoom", String(z));
      el.style.setProperty("--pan-x", `${panX}px`);
      el.style.setProperty("--pan-y", `${panY}px`);
    },
    [canvasRef],
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

      // getBoundingClientRect returns the post-transform bounding box, so
      // rect.width = containerFitW × oldZoom (and same for height). We
      // derive the container-fit size implicitly instead of taking it
      // as a prop.
      const rect = el.getBoundingClientRect();
      const oldRenderedW = rect.width;
      const oldRenderedH = rect.height;
      if (oldRenderedW <= 0 || oldRenderedH <= 0) return;

      const fracX = (e.clientX - rect.left) / oldRenderedW;
      const fracY = (e.clientY - rect.top) / oldRenderedH;

      const scale = newZoom / oldZoom;
      const newRenderedW = oldRenderedW * scale;
      const newRenderedH = oldRenderedH * scale;

      // Anchor math tied to CSS transform-origin `50% 0` on the canvas:
      // horizontal scale grows from the box's horizontal center, so the
      // invariant point under a pure scale is fracX = 0.5. Vertical
      // scale grows from y=0 (top), so vertical math is unchanged.
      panXRef.current += (fracX - 0.5) * (newRenderedW - oldRenderedW);
      panYRef.current += fracY * (newRenderedH - oldRenderedH);

      publish(newZoom, panXRef.current, panYRef.current);

      setZoom(newZoom);
      zoomRef.current = newZoom;
      localStorage.setItem(key, String(newZoom));
    },
    [publish, min, max, step, key, canvasRef],
  );

  return { zoom, adjustBy, setZoomTo, onWheel };
}

function clamp(n: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, n));
}
