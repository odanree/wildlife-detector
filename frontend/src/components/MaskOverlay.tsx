import { type MouseEvent as ReactMouseEvent, useRef, useState } from "react";
import type { Rect } from "../api/masks";
import styles from "./MaskOverlay.module.css";

export type MaskMode = "idle" | "edit";

interface MaskOverlayProps {
  baseW: number;
  baseH: number;
  masks: Rect[];
  mode: MaskMode;
  onChange: (masks: Rect[]) => void;
}

const MIN_MASK_PX = 8;
const DELETE_RADIUS = 12;

/**
 * SVG overlay for OSD-mask rectangle editing. Ports the vanilla-JS
 * mask editor as-is:
 *
 *   - Single edit mode (no separate "tweak" — masks are treated as
 *     immutable-once-drawn; wrong ones are deleted and re-drawn)
 *   - Draw: click-drag on empty space to create a new rectangle
 *   - Delete: click the red × handle at each rectangle's top-right
 *   - Idle: read-only; pointer-events pass through so wheel-zoom works
 *
 * Coordinate contract identical to ZoneOverlay: viewBox in image-pixel
 * space, so rects are stored/rendered in the space the backend
 * expects. Delete-handle geometry (r=12, cx/cy) matches vanilla.
 */
export function MaskOverlay({ baseW, baseH, masks, mode, onChange }: MaskOverlayProps) {
  const svgRef = useRef<SVGSVGElement>(null);
  const [draft, setDraft] = useState<Rect | null>(null);
  const draftStartRef = useRef<[number, number] | null>(null);

  const isEditing = mode === "edit";

  function eventToImagePoint(e: ReactMouseEvent): [number, number] | null {
    const svg = svgRef.current;
    if (!svg) return null;
    const rect = svg.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return null;
    const x = ((e.clientX - rect.left) / rect.width) * baseW;
    const y = ((e.clientY - rect.top) / rect.height) * baseH;
    return [Math.round(x), Math.round(y)];
  }

  function onSvgMouseDown(e: ReactMouseEvent) {
    if (!isEditing) return;
    // A click on a delete handle bubbles up to the svg, but the handle's
    // own onMouseDown stops propagation, so we won't hit this path there.
    const pt = eventToImagePoint(e);
    if (!pt) return;
    draftStartRef.current = pt;
    setDraft([pt[0], pt[1], pt[0], pt[1]]);
  }

  function onSvgMouseMove(e: ReactMouseEvent) {
    if (!isEditing || !draftStartRef.current) return;
    const pt = eventToImagePoint(e);
    if (!pt) return;
    const [sx, sy] = draftStartRef.current;
    setDraft([sx, sy, pt[0], pt[1]]);
  }

  function onSvgMouseUp() {
    if (!isEditing) return;
    if (draft) {
      const r = normalizeRect(draft);
      if (r[2] - r[0] >= MIN_MASK_PX && r[3] - r[1] >= MIN_MASK_PX) {
        onChange([...masks, r]);
      }
    }
    draftStartRef.current = null;
    setDraft(null);
  }

  function onSvgMouseLeave() {
    draftStartRef.current = null;
    setDraft(null);
  }

  function onDeleteMouseDown(idx: number, e: ReactMouseEvent) {
    if (!isEditing) return;
    e.stopPropagation();
    onChange(masks.filter((_, i) => i !== idx));
  }

  const rubberBand = draft ? normalizeRect(draft) : null;

  const svgClass = [
    styles.svg,
    isEditing ? styles.svgInteractive : "",
    isEditing ? styles.svgDraw : "",
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <svg
      ref={svgRef}
      className={svgClass}
      viewBox={`0 0 ${baseW} ${baseH}`}
      preserveAspectRatio="none"
      role="img"
      aria-label={`OSD mask editor, mode ${mode}, ${masks.length} rectangles`}
      onMouseDown={onSvgMouseDown}
      onMouseMove={onSvgMouseMove}
      onMouseUp={onSvgMouseUp}
      onMouseLeave={onSvgMouseLeave}
    >
      <title>OSD mask editor</title>
      {masks.map((r, i) => {
        const [x1, y1, x2, y2] = r;
        return (
          // biome-ignore lint/suspicious/noArrayIndexKey: mask index IS the identity for delete ops
          <g key={i}>
            <rect
              className={`${styles.mask} ${isEditing ? styles.maskEditing : ""}`}
              x={x1}
              y={y1}
              width={x2 - x1}
              height={y2 - y1}
            />
            {isEditing && (
              <>
                <circle
                  className={styles.delete}
                  cx={x2}
                  cy={y1}
                  r={DELETE_RADIUS}
                  onMouseDown={(e) => onDeleteMouseDown(i, e)}
                />
                <text
                  className={styles.deleteX}
                  x={x2}
                  y={y1}
                  textAnchor="middle"
                  dominantBaseline="central"
                  onMouseDown={(e) => onDeleteMouseDown(i, e)}
                >
                  ×
                </text>
              </>
            )}
          </g>
        );
      })}
      {rubberBand && (
        <rect
          className={styles.rubberband}
          x={rubberBand[0]}
          y={rubberBand[1]}
          width={rubberBand[2] - rubberBand[0]}
          height={rubberBand[3] - rubberBand[1]}
        />
      )}
    </svg>
  );
}

function normalizeRect(r: Rect): Rect {
  const [a, b, c, d] = r;
  return [Math.min(a, c), Math.min(b, d), Math.max(a, c), Math.max(b, d)];
}
