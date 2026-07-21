import { useStatus } from "../hooks/useStatus";
import { Chip } from "./Chip";
import styles from "./GateFunnelChip.module.css";

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
  const velRej = g.motion_velocity_rejected ?? 0;
  const perRej = g.motion_persistence_rejected ?? 0;
  const kinRej = velRej + perRej;
  return (
    <Chip
      label="gate"
      title={`Motion → zone → baseline-passed → vlm → hit. Ratios show which stage filters the most; the drop between stages is the interesting signal.${
        kinRej > 0
          ? ` Kinematic pre-filter (before motion counter): velocity ×${velRej}, persistence ×${perRej}.`
          : ""
      }`}
    >
      {kinRej > 0 && (
        <>
          <span className={styles.kin}>
            kin −{velRej}v/−{perRej}p
          </span>
          <span className={styles.arrow}>→</span>
        </>
      )}
      motion <b className={styles.b}>{g.motion_events}</b>
      <span className={styles.arrow}>→</span>
      zone <b className={styles.b}>{g.zone_events}</b>
      <span className={styles.arrow}>→</span>
      base <b className={styles.b}>{basePassed}</b>
      <span className={styles.arrow}>→</span>
      vlm <b className={styles.b}>{g.vlm_calls}</b>
      <span className={styles.arrow}>→</span>
      hit <b className={styles.hit}>{g.vlm_confirmed}</b>
    </Chip>
  );
}
