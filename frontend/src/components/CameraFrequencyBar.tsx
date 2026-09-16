import { useMemo } from "react";
import styles from "./CameraFrequencyBar.module.css";

interface CameraFrequencyBarProps {
  /** camera_id → percent (0-100). */
  frequency: Record<string, number>;
  /** Total alert count, for the tooltip's absolute numbers. */
  total?: number;
}

/**
 * Horizontal bar per camera: what share of this rat's alerts each
 * camera contributed. Single series, single hue (the camera-badge
 * accent), labels in text tokens — so no legend, and the bar carries
 * magnitude only. Plain CSS flex; a chart library for five bars would
 * be dead weight in the bundle.
 *
 * Sorted by share descending so the dominant camera is always the top
 * row — the question this answers is "is this rat really cross-camera,
 * or 96% one camera with a stray link?" (2b flagged 3/6 crawlspace rats
 * as unverified cross-camera links; this is how the operator eyeballs
 * them before reaching for Merge).
 */
export function CameraFrequencyBar({ frequency, total }: CameraFrequencyBarProps) {
  const rows = useMemo(
    () =>
      Object.entries(frequency)
        .map(([cam, pct]) => ({ cam, pct }))
        .sort((a, b) => b.pct - a.pct || a.cam.localeCompare(b.cam)),
    [frequency],
  );
  if (rows.length === 0) return <div className={styles.empty}>no alerts</div>;
  const max = Math.max(...rows.map((r) => r.pct), 1);

  return (
    <div
      className={styles.chart}
      role="img"
      aria-label={`camera share: ${rows.map((r) => `${r.cam} ${r.pct}%`).join(", ")}`}
    >
      {rows.map((r) => {
        const n = total != null ? Math.round((r.pct / 100) * total) : null;
        return (
          <div
            key={r.cam}
            className={styles.row}
            title={n != null ? `${r.cam}: ${r.pct}% (${n} alerts)` : `${r.cam}: ${r.pct}%`}
          >
            <span className={styles.label}>{r.cam}</span>
            <div className={styles.track}>
              {/* width relative to the largest bar so a 96/4 split still
                  shows the 4% as a visible sliver instead of a hairline */}
              <div className={styles.fill} style={{ width: `${(r.pct / max) * 100}%` }} />
            </div>
            <span className={styles.pct}>{r.pct.toFixed(r.pct < 10 ? 1 : 0)}%</span>
          </div>
        );
      })}
    </div>
  );
}
