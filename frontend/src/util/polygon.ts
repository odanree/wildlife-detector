import type { Point } from "../api/zone";

/** Signed area × 2 of the triangle (p, q, r). Positive = CCW, negative = CW,
 *  zero = collinear. Used by the segment-intersection test below. */
function ccw(p: Point, q: Point, r: Point): number {
  return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0]);
}

/** Strict segment intersection: true when segments (a-b) and (c-d) cross in
 *  their interiors. Endpoints touching / collinear overlap return false —
 *  that's the correct semantics for polygon simplicity (adjacent edges must
 *  share a vertex without counting as an intersection). */
function segmentsIntersect(a: Point, b: Point, c: Point, d: Point): boolean {
  const d1 = ccw(c, d, a);
  const d2 = ccw(c, d, b);
  const d3 = ccw(a, b, c);
  const d4 = ccw(a, b, d);
  return ((d1 > 0 && d2 < 0) || (d1 < 0 && d2 > 0)) && ((d3 > 0 && d4 < 0) || (d3 < 0 && d4 > 0));
}

/**
 * Check whether a polygon is simple (non-self-intersecting).
 *
 * A closed polygon is simple when no pair of non-adjacent edges cross.
 * Triangles are always simple; O(n²) test for n ≥ 4 edges. n is small in
 * this app (usually < 20 vertices per zone), so no performance concern.
 *
 * Why this matters: SVG draws polygons by stroking edges in vertex order.
 * A polygon with V10 placed inside the outline of V0..V9 renders the
 * closing edge V10→V0 as a diagonal through the interior — visually
 * indistinguishable from "two zones." Blocking save on non-simple
 * geometry is cheaper than teaching users to think about winding order.
 */
export function polygonIsSimple(pts: readonly Point[]): boolean {
  const n = pts.length;
  if (n < 4) return true; // triangles can't self-intersect
  for (let i = 0; i < n; i++) {
    const a = pts[i];
    const b = pts[(i + 1) % n];
    for (let j = i + 2; j < n; j++) {
      // Skip the wrap-around adjacency (edge i=0 and edge j=n-1 share
      // vertex 0), which would otherwise trigger a false positive.
      if (i === 0 && j === n - 1) continue;
      const c = pts[j];
      const d = pts[(j + 1) % n];
      if (segmentsIntersect(a, b, c, d)) return false;
    }
  }
  return true;
}
