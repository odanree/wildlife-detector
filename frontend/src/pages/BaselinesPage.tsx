import { Link } from "react-router-dom";
import { type BaselineMeta, type BaselineSlot, baselineImageUrl } from "../api/baseline";
import { useBaselineMeta } from "../hooks/useBaselineMeta";
import { useCameras } from "../hooks/useCameras";
import { fmtRelative } from "../util/time";

/**
 * Baselines page — grid of every camera × mode (day/night) pair.
 * Read-only view; capture/clear live on the live-preview page next to
 * the actual stream (operator needs the current frame to decide what
 * "clean baseline" means).
 *
 * Pattern: strangler-fig continuation — same shape as the alerts
 * migration. /baselines still serves the vanilla template today; a
 * follow-up cutover PR (small, once we're confident) 302s /baselines
 * → /react/baselines and deletes _BASELINES_HTML.
 */
export function BaselinesPage() {
  const camerasResp = useCameras();
  const cameras = camerasResp.data?.cameras ?? [];

  return (
    <div style={styles.wrap}>
      <header style={styles.header}>
        <Link to="/" style={styles.title}>
          wildlife-detector — baselines
        </Link>
        <span style={styles.blurb}>
          {cameras.length} camera{cameras.length === 1 ? "" : "s"} · day + night per camera
        </span>
        <Link to="/" style={styles.closeBtn} aria-label="Back to live preview">
          ×
        </Link>
      </header>
      <div style={styles.grid}>
        {cameras.flatMap((camera) => [
          <BaselineCell key={`${camera}:day`} camera={camera} mode="day" />,
          <BaselineCell key={`${camera}:night`} camera={camera} mode="night" />,
        ])}
        {cameras.length === 0 && (
          <div style={styles.empty}>Waiting for the camera roster from /api/cameras…</div>
        )}
      </div>
    </div>
  );
}

function BaselineCell({ camera, mode }: { camera: string; mode: "day" | "night" }) {
  const { data, error } = useBaselineMeta(camera);
  const slot = data?.[mode];
  return (
    <div style={styles.cell}>
      <div style={styles.cellHeader}>
        <span>
          <b>{camera}</b> · {mode}
        </span>
        <SlotStatus data={data} slot={slot} error={error} />
      </div>
      {slot?.exists && data ? (
        <img
          style={styles.img}
          src={baselineImageUrl(camera, mode, data.version)}
          alt={`${camera} ${mode} baseline`}
        />
      ) : (
        <div style={styles.placeholder}>
          {error ? `error: ${error.message}` : "no baseline captured yet"}
        </div>
      )}
      {slot?.exists && (
        <div style={styles.meta}>
          captured {fmtRelative(slot.ts)} · {(slot.bytes / 1024).toFixed(0)} KB
        </div>
      )}
    </div>
  );
}

function SlotStatus({
  data,
  slot,
  error,
}: {
  data: BaselineMeta | null;
  slot: BaselineSlot | undefined;
  error: Error | null;
}) {
  if (error) return <span style={{ color: "#f88", fontSize: 11 }}>error</span>;
  if (!data) return <span style={{ color: "#667", fontSize: 11 }}>loading…</span>;
  if (!slot?.exists) return <span style={styles.missing}>missing</span>;
  const ageSec = Date.now() / 1000 - slot.ts;
  if (ageSec < 600) return <span style={styles.fresh}>fresh</span>;
  return null;
}

const styles = {
  wrap: {
    minHeight: "100vh",
    background: "#0f0f13",
    color: "#eef",
    fontFamily: "-apple-system, 'Segoe UI', sans-serif",
  },
  header: {
    display: "flex",
    gap: 24,
    padding: "8px 16px",
    fontSize: 13,
    borderBottom: "1px solid #2a2a30",
    background: "#16161a",
    alignItems: "center",
  },
  title: { textDecoration: "none", color: "#eef", fontWeight: 600 },
  blurb: { color: "#9aa" },
  closeBtn: {
    background: "#26262c",
    color: "#ddd",
    border: "1px solid #3a3a40",
    padding: "4px 10px",
    borderRadius: 4,
    fontSize: 14,
    textDecoration: "none",
    lineHeight: 1,
    marginLeft: "auto",
    fontWeight: 600,
  },
  grid: {
    padding: 16,
    display: "grid",
    gap: 16,
    gridTemplateColumns: "repeat(2, minmax(0, 1fr))",
  },
  cell: {
    background: "#1a1a20",
    border: "1px solid #26262c",
    borderRadius: 6,
    overflow: "hidden" as const,
    display: "flex",
    flexDirection: "column" as const,
  },
  cellHeader: {
    padding: "8px 12px",
    fontSize: 12,
    color: "#9aa",
    background: "#14141a",
    borderBottom: "1px solid #26262c",
    display: "flex",
    justifyContent: "space-between",
    alignItems: "center",
  },
  img: { display: "block", width: "100%", height: "auto", background: "#000" },
  placeholder: {
    padding: "60px 16px",
    textAlign: "center" as const,
    color: "#667",
    fontSize: 12,
    background: "#0a0a10",
  },
  meta: {
    padding: "6px 12px",
    fontSize: 11,
    color: "#667",
    background: "#14141a",
    borderTop: "1px solid #26262c",
  },
  empty: { padding: 40, textAlign: "center" as const, color: "#667", gridColumn: "1 / -1" },
  missing: { color: "#f66", fontSize: 11 },
  fresh: { color: "#6f6", fontSize: 11 },
} as const;
