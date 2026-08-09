import type { SlewPresetEntry } from "../api/slewPresets";
import styles from "./SlewPresetsOverlay.module.css";

interface SlewPresetsOverlayProps {
  baseW: number;
  baseH: number;
  presets: SlewPresetEntry[];
  /** When set, this preset is being edited by ZoneOverlay in a
   *  separate SVG layer — skip it here to avoid a double-outline. */
  activePreset: number | null;
}

/**
 * Read-only outlines of all slew presets EXCEPT the one currently
 * being edited (that one is rendered by ZoneOverlay). Each polygon
 * shows a preset-number badge at its centroid so the operator can
 * tell which region maps to which preset without opening the panel.
 *
 * Coord contract: same as ZoneOverlay — viewBox = (0 0 baseW baseH),
 * polygon points in pixel coords. Mouse-transparent (pointer-events
 * none) so it doesn't block clicks on the underlying preview or the
 * ZoneOverlay editing surface.
 */
export function SlewPresetsOverlay({
  baseW,
  baseH,
  presets,
  activePreset,
}: SlewPresetsOverlayProps) {
  const visible = presets.filter((p) => p.polygon.length >= 3 && p.preset !== activePreset);
  if (visible.length === 0) return null;
  return (
    <svg
      className={styles.svg}
      viewBox={`0 0 ${baseW} ${baseH}`}
      preserveAspectRatio="none"
      role="img"
      aria-label={`Slew presets — ${visible.length} zones`}
    >
      <title>Slew preset zones</title>
      {visible.map((p) => {
        const points = p.polygon.map((pt) => pt.join(",")).join(" ");
        const c = centroid(p.polygon);
        return (
          <g key={p.preset}>
            <polygon className={styles.polygon} points={points} />
            <text
              className={styles.label}
              x={c[0]}
              y={c[1]}
              textAnchor="middle"
              dominantBaseline="middle"
            >
              {`P${p.preset}`}
            </text>
            <text
              className={styles.labelSub}
              x={c[0]}
              y={c[1] + Math.max(24, baseH * 0.03)}
              textAnchor="middle"
            >
              {p.name}
            </text>
          </g>
        );
      })}
    </svg>
  );
}

function centroid(poly: readonly [number, number][]): [number, number] {
  let sx = 0;
  let sy = 0;
  for (const [x, y] of poly) {
    sx += x;
    sy += y;
  }
  return [sx / poly.length, sy / poly.length];
}
