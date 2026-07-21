import { type MouseEvent as ReactMouseEvent, useRef, useState } from "react";
import type { Point } from "../api/zone";
import styles from "./ZoneOverlay.module.css";

export type EditMode = "idle" | "draw" | "tweak";

interface ZoneOverlayProps {
  baseW: number;
  baseH: number;
  polygon: Point[];
  mode: EditMode;
  onChange: (polygon: Point[]) => void;
  /** Called when the draw-mode close-loop happens (click first vertex). */
  onClose: () => void;
}

const CLOSE_THRESHOLD_FRAC = 0.02;

/**
 * SVG overlay for zone polygon editing. Handles all three modes:
 *
 * - **idle**: renders the current polygon read-only, no interaction.
 * - **draw**: click canvas to add vertex. Rubber-band line follows
 *   the cursor from the last vertex. Click near the first vertex
 *   (≥3 vertices) to close the loop → onClose().
 * - **tweak**: drag vertices to move them. Right-click a vertex to
 *   remove (min 3 vertices enforced). No new vertices added.
 *
 * Coordinate space: SVG viewBox is (0 0 baseW baseH) so polygon
 * points are stored + read in image-pixel coords directly. No
 * client-side ↔ image conversion needed inside this component.
 * getBoundingClientRect maps DOM screen coords back to image coords
 * for the mouse handlers.
 *
 * Pattern: SVG viewBox as coordinate contract. The parent's canvas
 * size can change (zoom, camera switch, INPUT_WIDTH env override)
 * without any polygon-recomputation. SVG scales the geometry for us.
 */
export function ZoneOverlay({ baseW, baseH, polygon, mode, onChange, onClose }: ZoneOverlayProps) {
  const svgRef = useRef<SVGSVGElement>(null);
  const [cursorImg, setCursorImg] = useState<Point | null>(null);
  const [dragIdx, setDragIdx] = useState<number | null>(null);

  const isEditing = mode !== "idle";
  const vertexRadius = Math.max(6, baseW * 0.006);
  const closeThreshold = baseW * CLOSE_THRESHOLD_FRAC;

  function eventToImagePoint(e: ReactMouseEvent): Point | null {
    const svg = svgRef.current;
    if (!svg) return null;
    const rect = svg.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return null;
    const x = ((e.clientX - rect.left) / rect.width) * baseW;
    const y = ((e.clientY - rect.top) / rect.height) * baseH;
    return [Math.round(x), Math.round(y)];
  }

  function onSvgClick(e: ReactMouseEvent) {
    const pt = eventToImagePoint(e);
    if (!pt) return;
    if (mode === "draw") {
      // Close-loop if clicking near the first vertex and we have ≥3
      if (polygon.length >= 3) {
        const [fx, fy] = polygon[0];
        const dx = pt[0] - fx;
        const dy = pt[1] - fy;
        if (Math.hypot(dx, dy) <= closeThreshold) {
          onClose();
          return;
        }
      }
      onChange([...polygon, pt]);
      return;
    }
    if (mode === "tweak" && polygon.length >= 2) {
      // Edge-click → insert a new vertex splitting that edge. First
      // guard: if the click landed near an existing vertex, do nothing
      // — the vertex's own handlers already own that pixel area for
      // drag / right-click-delete. Second: if the click is close to
      // an edge (within 2× vertex radius so hit targets are generous),
      // insert a new vertex at the projection point.
      const nearVertex = polygon.some(
        (v) => Math.hypot(v[0] - pt[0], v[1] - pt[1]) <= vertexRadius * 1.5,
      );
      if (nearVertex) return;
      const hit = nearestEdge(polygon, pt);
      if (hit && hit.dist <= vertexRadius * 2) {
        const next = polygon.slice();
        next.splice(hit.insertAt, 0, hit.projection);
        onChange(next);
      }
    }
  }

  function onSvgMouseMove(e: ReactMouseEvent) {
    if (mode === "draw") {
      const pt = eventToImagePoint(e);
      if (pt) setCursorImg(pt);
    }
    if (mode === "tweak" && dragIdx !== null) {
      const pt = eventToImagePoint(e);
      if (pt) {
        const next = polygon.slice();
        next[dragIdx] = pt;
        onChange(next);
      }
    }
  }

  function onSvgMouseLeave() {
    setCursorImg(null);
    setDragIdx(null);
  }

  function onVertexMouseDown(idx: number, e: ReactMouseEvent) {
    if (mode !== "tweak") return;
    e.stopPropagation();
    setDragIdx(idx);
  }

  function onVertexMouseUp() {
    if (dragIdx !== null) setDragIdx(null);
  }

  function onVertexContextMenu(idx: number, e: ReactMouseEvent) {
    if (mode !== "tweak") return;
    e.preventDefault();
    e.stopPropagation();
    if (polygon.length <= 3) return; // Enforce triangle minimum
    onChange(polygon.filter((_, i) => i !== idx));
  }

  const polygonPoints = polygon.map((p) => p.join(",")).join(" ");
  const rubberBand =
    mode === "draw" && cursorImg && polygon.length > 0
      ? {
          x1: polygon[polygon.length - 1][0],
          y1: polygon[polygon.length - 1][1],
          x2: cursorImg[0],
          y2: cursorImg[1],
        }
      : null;

  const canClose =
    mode === "draw" && polygon.length >= 3 && cursorImg
      ? Math.hypot(cursorImg[0] - polygon[0][0], cursorImg[1] - polygon[0][1]) <= closeThreshold
      : false;

  const svgClass = [
    styles.svg,
    isEditing ? styles.svgInteractive : "",
    mode === "draw" ? styles.svgDraw : "",
    dragIdx !== null ? styles.vertexDragging : "",
  ]
    .filter(Boolean)
    .join(" ");

  return (
    // Zone editing is inherently a mouse-drives-cursor-position interaction —
    // there's no meaningful keyboard equivalent for "click here on the video
    // to add a vertex at these image pixels." A11y still gets a <title> for
    // screen-reader labelling.
    // biome-ignore lint/a11y/useKeyWithClickEvents: mouse-only affordance; screen-reader alternative would be a separate coord-entry form, not a keyboard equivalent
    <svg
      ref={svgRef}
      className={svgClass}
      viewBox={`0 0 ${baseW} ${baseH}`}
      preserveAspectRatio="none"
      role="img"
      aria-label={`Zone polygon editor, mode ${mode}, ${polygon.length} vertices`}
      onClick={onSvgClick}
      onMouseMove={onSvgMouseMove}
      onMouseLeave={onSvgMouseLeave}
      onMouseUp={onVertexMouseUp}
    >
      <title>Zone polygon editor</title>
      {polygon.length >= 2 && (
        <polygon
          className={isEditing ? styles.polygonEditing : styles.polygon}
          points={polygonPoints}
        />
      )}
      {rubberBand && (
        <line
          className={styles.rubberband}
          x1={rubberBand.x1}
          y1={rubberBand.y1}
          x2={rubberBand.x2}
          y2={rubberBand.y2}
        />
      )}
      {polygon.map((p, i) => {
        const isFirstAndClosable = mode === "draw" && i === 0 && polygon.length >= 3 && canClose;
        const cls = [
          styles.vertex,
          isEditing ? styles.vertexEditing : "",
          isFirstAndClosable ? styles.vertexClosable : "",
        ]
          .filter(Boolean)
          .join(" ");
        return (
          // biome-ignore lint/suspicious/noArrayIndexKey: polygon vertices have no stable identity — index IS the identity for edit ops
          <g key={i}>
            <circle
              className={cls}
              cx={p[0]}
              cy={p[1]}
              r={vertexRadius}
              onMouseDown={(e) => onVertexMouseDown(i, e)}
              onContextMenu={(e) => onVertexContextMenu(i, e)}
            />
            {isEditing && (
              // Vertex index label — helps identify which vertex to delete
              // when a polygon looks visually wrong (e.g. a concave notch
              // caused by one vertex placed inside the outer envelope).
              // Only shown while editing so read-only view stays clean.
              <text
                className={styles.vertexLabel}
                x={p[0]}
                y={p[1] - vertexRadius - 4}
                textAnchor="middle"
              >
                {i}
              </text>
            )}
          </g>
        );
      })}
    </svg>
  );
}

/** Find the nearest polygon edge to a point in image-pixel space and
 *  return the split-insertion payload. Returns null for polygons with
 *  fewer than 2 vertices.
 *
 *  insertAt is the array index at which to splice the new vertex so
 *  the polygon topology stays consistent: edge i connects vertex i to
 *  vertex (i+1) % n, so inserting the new vertex at position i+1
 *  places it between the two endpoints of edge i. */
function nearestEdge(
  polygon: readonly Point[],
  pt: Point,
): { dist: number; projection: Point; insertAt: number } | null {
  const n = polygon.length;
  if (n < 2) return null;
  let best: { dist: number; projection: Point; insertAt: number } | null = null;
  for (let i = 0; i < n; i++) {
    const a = polygon[i];
    const b = polygon[(i + 1) % n];
    const dx = b[0] - a[0];
    const dy = b[1] - a[1];
    const lenSq = dx * dx + dy * dy;
    if (lenSq === 0) continue;
    let t = ((pt[0] - a[0]) * dx + (pt[1] - a[1]) * dy) / lenSq;
    t = Math.max(0, Math.min(1, t));
    const proj: Point = [Math.round(a[0] + t * dx), Math.round(a[1] + t * dy)];
    const d = Math.hypot(pt[0] - proj[0], pt[1] - proj[1]);
    if (best === null || d < best.dist) {
      best = { dist: d, projection: proj, insertAt: i + 1 };
    }
  }
  return best;
}
