import { type BaselineMeta, type BaselineSlot, baselineImageUrl } from "../api/baseline";
import { GlobalHeader } from "../components/GlobalHeader";
import { useBaselineMeta } from "../hooks/useBaselineMeta";
import { useCameras } from "../hooks/useCameras";
import { fmtRelative } from "../util/time";
import styles from "./BaselinesPage.module.css";

/**
 * Baselines page — grid of every camera × mode (day/night) pair.
 * Read-only view; capture/clear live on the live-preview page next to
 * the actual stream (operator needs the current frame to decide what
 * "clean baseline" means).
 */
export function BaselinesPage() {
  const camerasResp = useCameras();
  const cameras = camerasResp.data?.cameras ?? [];

  return (
    <div className={styles.wrap}>
      <GlobalHeader
        right={
          <span className={styles.blurb}>
            {cameras.length} camera{cameras.length === 1 ? "" : "s"} · day + night per camera
          </span>
        }
      />
      <div className={styles.grid}>
        {cameras.flatMap((camera) => [
          <BaselineCell key={`${camera}:day`} camera={camera} mode="day" />,
          <BaselineCell key={`${camera}:night`} camera={camera} mode="night" />,
        ])}
        {cameras.length === 0 && (
          <div className={styles.empty}>Waiting for the camera roster from /api/cameras…</div>
        )}
      </div>
    </div>
  );
}

function BaselineCell({ camera, mode }: { camera: string; mode: "day" | "night" }) {
  const { data, error } = useBaselineMeta(camera);
  const slot = data?.[mode];
  return (
    <div className={styles.cell}>
      <div className={styles.cellHeader}>
        <span>
          <b>{camera}</b> · {mode}
        </span>
        <SlotStatus data={data} slot={slot} error={error} />
      </div>
      {slot?.exists && data ? (
        <img
          className={styles.img}
          src={baselineImageUrl(camera, mode, data.version)}
          alt={`${camera} ${mode} baseline`}
        />
      ) : (
        <div className={styles.placeholder}>
          {error ? `error: ${error.message}` : "no baseline captured yet"}
        </div>
      )}
      {slot?.exists && (
        <div className={styles.meta}>
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
  if (error) return <span className={styles.err}>error</span>;
  if (!data) return <span className={styles.loading}>loading…</span>;
  if (!slot?.exists) return <span className={styles.missing}>missing</span>;
  const ageSec = Date.now() / 1000 - slot.ts;
  if (ageSec < 600) return <span className={styles.fresh}>fresh</span>;
  return null;
}
