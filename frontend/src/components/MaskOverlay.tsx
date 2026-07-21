import { type MouseEvent as ReactMouseEvent, useRef, useState } from "react";
import type { Rect } from "../api/masks";
import styles from "./MaskOverlay.module.css";

export type MaskMode = "idle" | "draw" | "tweak";

interface MaskOverlayProps {
  baseW: number;
  baseH: number;
  masks: Rect[];
  mode: MaskMode;
  onChange: (masks: Rect[]) => void;
}

type DragState =
  | { kind: "draw"; startX: number; startY: number; curX: number; curY: number }
  | { kind: "move"; idx: number; anchorX: number; anchorY: number; orig: Rect }
  | { kind: "resize"; idx: number; corner: "nw" | "ne" | "sw" | "se"; orig: Rect }
  | null;

const MIN_MASK_PX = 8;

/**
 * SVG overlay for OSD-mask rectangle editing. Ports the vanilla-JS
 * mask editor. Same coordinate contract as ZoneOverlay: viewBox in
 * image-pixel coords so rectangles are stored/rendered in the space
 * the backend expects.
 *
 * Modes:
 *   - idle:  read-only; rectangles rendered with pointer-events: none
 *            so wheel zoom passes through
 *   - draw:  click-drag on empty space to create a new rectangle
 *   - tweak: drag rectangle body to move; drag any corner handle to
 *            resize; right-click rectangle body to delete
 *
 * Rectangles are stored as [x1, y1, x2, y2] with x1<x2, y1<y2
 * normalized. Drag-out during a resize/draw operation swaps corners
 * so the invariant holds on save.
 */
export function MaskOverlay({ baseW, baseH, masks, mode, onChange }: MaskOverlayProps) {
  const svgRef = useRef<SVGSVGElement>(null);
  const [drag, setDrag] = useState<DragState>(null);

  const isEditing = mode !== "idle";

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
    if (mode !== "draw") return;
    const pt = eventToImagePoint(e);
    if (!pt) return;
    setDrag({ kind: "draw", startX: pt[0], startY: pt[1], curX: pt[0], curY: pt[1] });
  }

  function onSvgMouseMove(e: ReactMouseEvent) {
    if (!drag) return;
    const pt = eventToImagePoint(e);
    if (!pt) return;
    if (drag.kind === "draw") {
      setDrag({ ...drag, curX: pt[0], curY: pt[1] });
    } else if (drag.kind === "move") {
      const dx = pt[0] - drag.anchorX;
      const dy = pt[1] - drag.anchorY;
      const [x1, y1, x2, y2] = drag.orig;
      const next = masks.slice();
      next[drag.idx] = [x1 + dx, y1 + dy, x2 + dx, y2 + dy];
      onChange(next);
    } else if (drag.kind === "resize") {
      const [x1, y1, x2, y2] = drag.orig;
      let nx1 = x1;
      let ny1 = y1;
      let nx2 = x2;
      let ny2 = y2;
      if (drag.corner === "nw") {
        nx1 = pt[0];
        ny1 = pt[1];
      } else if (drag.corner === "ne") {
        nx2 = pt[0];
        ny1 = pt[1];
      } else if (drag.corner === "sw") {
        nx1 = pt[0];
        ny2 = pt[1];
      } else {
        nx2 = pt[0];
        ny2 = pt[1];
      }
      const next = masks.slice();
      next[drag.idx] = normalizeRect([nx1, ny1, nx2, ny2]);
      onChange(next);
    }
  }

  function onSvgMouseUp() {
    if (!drag) return;
    if (drag.kind === "draw") {
      const r = normalizeRect([drag.startX, drag.startY, drag.curX, drag.curY]);
      if (r[2] - r[0] >= MIN_MASK_PX && r[3] - r[1] >= MIN_MASK_PX) {
        onChange([...masks, r]);
      }
    }
    setDrag(null);
  }

  function onSvgMouseLeave() {
    setDrag(null);
  }

  function onMaskMouseDown(idx: number, e: ReactMouseEvent) {
    if (mode !== "tweak") return;
    e.stopPropagation();
    const pt = eventToImagePoint(e);
    if (!pt) return;
    setDrag({
      kind: "move",
      idx,
      anchorX: pt[0],
      anchorY: pt[1],
      orig: [...masks[idx]] as Rect,
    });
  }

  function onMaskContextMenu(idx: number, e: ReactMouseEvent) {
    if (mode !== "tweak") return;
    e.preventDefault();
    e.stopPropagation();
    onChange(masks.filter((_, i) => i !== idx));
  }

  function onHandleMouseDown(idx: number, corner: "nw" | "ne" | "sw" | "se", e: ReactMouseEvent) {
    if (mode !== "tweak") return;
    e.stopPropagation();
    setDrag({ kind: "resize", idx, corner, orig: [...masks[idx]] as Rect });
  }

  const rubberBand =
    drag?.kind === "draw" ? normalizeRect([drag.startX, drag.startY, drag.curX, drag.curY]) : null;

  const handleSize = Math.max(6, baseW * 0.006);

  const svgClass = [
    styles.svg,
    isEditing ? styles.svgInteractive : "",
    mode === "draw" ? styles.svgDraw : "",
    drag?.kind === "move" || drag?.kind === "resize" ? styles.maskDragging : "",
  ]
    .filter(Boolean)
    .join(" ");

  return (
    // Same a11y stance as ZoneOverlay — mouse-only affordance for
    // cursor-driven rectangle drawing.
    // biome-ignore lint/a11y/useKeyWithClickEvents: mouse-only affordance; no keyboard equivalent for cursor-driven rectangle drawing
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
        const w = x2 - x1;
        const h = y2 - y1;
        return (
          // biome-ignore lint/suspicious/noArrayIndexKey: mask index IS the identity for edit ops
          <g key={i}>
            <rect
              className={`${styles.mask} ${isEditing ? styles.maskEditing : ""}`}
              x={x1}
              y={y1}
              width={w}
              height={h}
              onMouseDown={(e) => onMaskMouseDown(i, e)}
              onContextMenu={(e) => onMaskContextMenu(i, e)}
            />
            {mode === "tweak" && (
              <>
                <rect
                  className={styles.handle}
                  x={x1 - handleSize / 2}
                  y={y1 - handleSize / 2}
                  width={handleSize}
                  height={handleSize}
                  onMouseDown={(e) => onHandleMouseDown(i, "nw", e)}
                />
                <rect
                  className={`${styles.handle} ${styles.handleNE}`}
                  x={x2 - handleSize / 2}
                  y={y1 - handleSize / 2}
                  width={handleSize}
                  height={handleSize}
                  onMouseDown={(e) => onHandleMouseDown(i, "ne", e)}
                />
                <rect
                  className={`${styles.handle} ${styles.handleSW}`}
                  x={x1 - handleSize / 2}
                  y={y2 - handleSize / 2}
                  width={handleSize}
                  height={handleSize}
                  onMouseDown={(e) => onHandleMouseDown(i, "sw", e)}
                />
                <rect
                  className={styles.handle}
                  x={x2 - handleSize / 2}
                  y={y2 - handleSize / 2}
                  width={handleSize}
                  height={handleSize}
                  onMouseDown={(e) => onHandleMouseDown(i, "se", e)}
                />
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
