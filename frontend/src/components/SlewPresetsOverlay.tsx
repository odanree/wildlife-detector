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

// Warm/saturated palette that reads well against IR greyscale. Cycled
// by preset number so adjacent presets don't clash and the operator
// can tell "the yellow zone" vs "the green zone" without reading the
// label. Kept out of CSS so per-preset color assignment stays inline
// with the render pass.
const PRESET_COLORS = [
  "#ffcc4a", // amber — highest contrast against IR
  "#4ade80", // green
  "#f87171", // coral
  "#a78bfa", // violet
  "#22d3ee", // cyan
  "#fb923c", // orange
];

function colorForPreset(preset: number): string {
  return PRESET_COLORS[preset % PRESET_COLORS.length];
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
  // Scale label size with frame — 22 px on a 928 px frame = ~2.4% of
  // frame height. Same ratio at any resolution.
  const labelSize = Math.max(18, Math.round(baseH * 0.024));
  const strokeWidth = Math.max(3, Math.round(baseH * 0.004));
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
        const color = colorForPreset(p.preset);
        return (
          <g key={p.preset}>
            <polygon
              points={points}
              fill={color}
              fillOpacity={0.14}
              stroke={color}
              strokeWidth={strokeWidth}
              strokeDasharray={`${strokeWidth * 3} ${strokeWidth * 2}`}
            />
            <text
              x={c[0]}
              y={c[1]}
              textAnchor="middle"
              dominantBaseline="middle"
              fill={color}
              style={{
                fontFamily: "var(--font-mono, monospace)",
                fontSize: `${labelSize * 1.5}px`,
                fontWeight: 800,
                paintOrder: "stroke",
                stroke: "rgba(0, 0, 0, 0.85)",
                strokeWidth: strokeWidth,
              }}
            >
              {`P${p.preset}`}
            </text>
            <text
              x={c[0]}
              y={c[1] + labelSize * 1.5}
              textAnchor="middle"
              fill="rgba(255, 255, 255, 0.95)"
              style={{
                fontFamily: "var(--font-mono, monospace)",
                fontSize: `${labelSize * 0.75}px`,
                fontWeight: 600,
                paintOrder: "stroke",
                stroke: "rgba(0, 0, 0, 0.85)",
                strokeWidth: strokeWidth * 0.75,
              }}
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
