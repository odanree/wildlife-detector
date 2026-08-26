import { type MouseEvent as ReactMouseEvent, useRef, useState } from "react";
import styles from "./ManualDetectOverlay.module.css";

/**
 * SVG rubber-band rectangle for operator-drawn manual bboxes. Same
 * viewBox pattern as ZoneOverlay — SVG holds coordinates in detection-
 * frame pixel space, DOM screen coords convert via getBoundingClientRect.
 * That makes the bbox we POST to /api/manual-detect exactly the coord
 * space the detector's pipeline expects.
 *
 * Modes:
 * - `off`         : render nothing, no interaction (SVG is
 *                    `pointer-events: none` so the underlying MJPEG
 *                    stream still gets clicks — no interference).
 * - `armed`       : catch pointer events, draw rubber-band rect on
 *                    click-drag, fire onSubmit on mouseup.
 * - `submitting`  : freeze current rect (dim), don't accept new drags.
 */

const MIN_DIM_PX = 8; // Detector rejects < 8px per axis; enforce client-side too.

export type ManualDetectMode = "off" | "armed" | "submitting";

interface ManualDetectOverlayProps {
  baseW: number;
  baseH: number;
  mode: ManualDetectMode;
  onSubmit: (bbox: [number, number, number, number]) => void;
}

interface Rect {
  x1: number;
  y1: number;
  x2: number;
  y2: number;
}

export function ManualDetectOverlay({ baseW, baseH, mode, onSubmit }: ManualDetectOverlayProps) {
  const svgRef = useRef<SVGSVGElement>(null);
  const [rect, setRect] = useState<Rect | null>(null);
  const dragStart = useRef<{ x: number; y: number } | null>(null);

  const interactive = mode === "armed";

  function eventToImagePoint(e: ReactMouseEvent): { x: number; y: number } | null {
    const svg = svgRef.current;
    if (!svg) return null;
    const rect = svg.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return null;
    const x = ((e.clientX - rect.left) / rect.width) * baseW;
    const y = ((e.clientY - rect.top) / rect.height) * baseH;
    return {
      x: Math.max(0, Math.min(baseW, Math.round(x))),
      y: Math.max(0, Math.min(baseH, Math.round(y))),
    };
  }

  function onMouseDown(e: ReactMouseEvent) {
    if (!interactive) return;
    if (e.button !== 0) return; // left-click only
    e.preventDefault();
    const p = eventToImagePoint(e);
    if (!p) return;
    dragStart.current = p;
    setRect({ x1: p.x, y1: p.y, x2: p.x, y2: p.y });
  }

  function onMouseMove(e: ReactMouseEvent) {
    if (!interactive) return;
    if (!dragStart.current) return;
    const p = eventToImagePoint(e);
    if (!p) return;
    const s = dragStart.current;
    setRect({
      x1: Math.min(s.x, p.x),
      y1: Math.min(s.y, p.y),
      x2: Math.max(s.x, p.x),
      y2: Math.max(s.y, p.y),
    });
  }

  function onMouseUp(e: ReactMouseEvent) {
    if (!interactive) return;
    if (!dragStart.current || !rect) {
      dragStart.current = null;
      return;
    }
    const p = eventToImagePoint(e);
    const s = dragStart.current;
    dragStart.current = null;
    if (!p) {
      setRect(null);
      return;
    }
    const x1 = Math.min(s.x, p.x);
    const y1 = Math.min(s.y, p.y);
    const x2 = Math.max(s.x, p.x);
    const y2 = Math.max(s.y, p.y);
    const w = x2 - x1;
    const h = y2 - y1;
    if (w < MIN_DIM_PX || h < MIN_DIM_PX) {
      // Under-size — treat as accidental click, clear preview.
      setRect(null);
      return;
    }
    onSubmit([x1, y1, x2, y2]);
    // Keep the rect on-screen while submit is in flight so operator
    // sees what they submitted; parent clears our rect by transitioning
    // mode from `submitting` → `off` via useEffect (or via the
    // rect-clear implicit in the next mode change).
  }

  if (mode === "off") return null;

  return (
    <svg
      ref={svgRef}
      className={interactive ? styles.overlayArmed : styles.overlaySubmitting}
      viewBox={`0 0 ${baseW} ${baseH}`}
      preserveAspectRatio="none"
      role="img"
      aria-label="manual detection bbox overlay"
      onMouseDown={onMouseDown}
      onMouseMove={onMouseMove}
      onMouseUp={onMouseUp}
      onMouseLeave={onMouseUp}
    >
      {rect && (
        <rect
          x={rect.x1}
          y={rect.y1}
          width={Math.max(0, rect.x2 - rect.x1)}
          height={Math.max(0, rect.y2 - rect.y1)}
          className={mode === "submitting" ? styles.rectSubmitting : styles.rectArmed}
        />
      )}
    </svg>
  );
}
