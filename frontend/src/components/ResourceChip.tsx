import { useStatus } from "../hooks/useStatus";
import { fmtDuration, fmtMB, fmtPct } from "../util/format";
import { Chip } from "./Chip";

interface ResourceChipProps {
  camera: string;
}

/**
 * Detector process metrics: CPU % (multi-core, matches `docker stats`),
 * RSS memory, thread count, uptime. Peak values shown as secondary so
 * a settled reading still surfaces prior spikes for the operator.
 *
 * Pattern: SLI display — every field here is a signal the operator
 * needs to answer "is this container healthy?" without SSH.
 */
export function ResourceChip({ camera }: ResourceChipProps) {
  const { data, error } = useStatus(camera);
  if (error) return <Chip label="proc">error</Chip>;
  if (!data) return <Chip label="proc">…</Chip>;

  const r = data.resources;
  if (!r.available) {
    return <Chip label="proc">psutil unavailable</Chip>;
  }
  const cpu = r.cpu_pct ?? 0;
  const cpuPeak = r.cpu_peak_pct ?? 0;
  const rss = r.rss_mb ?? 0;
  const rssPeak = r.rss_peak_mb ?? 0;
  const cores = r.num_cpus ?? 0;
  return (
    <Chip
      label="proc"
      title={`Detector process on ${data.camera_id}. CPU is multi-core (0..${cores * 100}%), same as \`docker stats\`.`}
    >
      cpu <b style={styles.b}>{fmtPct(cpu)}</b>
      <span style={styles.dim}> / peak {fmtPct(cpuPeak)}</span>
      <span style={styles.sep}>·</span>
      mem <b style={styles.b}>{fmtMB(rss)}</b>
      <span style={styles.dim}> / peak {fmtMB(rssPeak)}</span>
      <span style={styles.sep}>·</span>
      up <b style={styles.b}>{fmtDuration(data.uptime_seconds)}</b>
    </Chip>
  );
}

const styles = {
  b: { color: "#ddd", fontWeight: 600, margin: "0 2px" },
  dim: { color: "#666" },
  sep: { color: "#555", margin: "0 6px" },
} as const;
