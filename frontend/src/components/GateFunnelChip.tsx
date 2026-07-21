import { useStatus } from "../hooks/useStatus";
import { Chip } from "./Chip";

interface GateFunnelChipProps {
  camera: string;
}

/**
 * Motion → zone → baseline → vlm → hit funnel counts. The ratios
 * between adjacent stages show which layer is doing the filtering
 * work — the primary "why isn't this detecting?" diagnostic on the
 * detector.
 *
 * baseline-passed is derived (zone_events - baseline_filtered) so the
 * chain reads left-to-right as "surviving each stage" not "rejected
 * at each stage." Cognitive load win.
 */
export function GateFunnelChip({ camera }: GateFunnelChipProps) {
  const { data, error } = useStatus(camera);
  if (error) return <Chip label="gate">error</Chip>;
  if (!data) return <Chip label="gate">…</Chip>;
  const g = data.gate_funnel;
  const basePassed = Math.max(0, g.zone_events - g.baseline_filtered);
  return (
    <Chip
      label="gate"
      title="Motion → zone → baseline-passed → vlm → hit. Ratios show which stage filters the most; the drop between stages is the interesting signal."
    >
      motion <b style={styles.b}>{g.motion_events}</b>
      <span style={styles.arrow}>→</span>
      zone <b style={styles.b}>{g.zone_events}</b>
      <span style={styles.arrow}>→</span>
      base <b style={styles.b}>{basePassed}</b>
      <span style={styles.arrow}>→</span>
      vlm <b style={styles.b}>{g.vlm_calls}</b>
      <span style={styles.arrow}>→</span>
      hit <b style={styles.hit}>{g.vlm_confirmed}</b>
    </Chip>
  );
}

const styles = {
  b: { color: "#ddd", fontWeight: 600, margin: "0 2px" },
  hit: { color: "#4d9", fontWeight: 600, margin: "0 2px" },
  arrow: { color: "#555", margin: "0 4px" },
} as const;
