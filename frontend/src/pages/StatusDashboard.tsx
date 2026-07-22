import { GlobalHeader } from "../components/GlobalHeader";
import { StatusBar } from "../components/StatusBar";
import { useCameras } from "../hooks/useCameras";
import styles from "./StatusDashboard.module.css";

/**
 * Multi-camera operations dashboard. One StatusBar per detector.
 *
 * Doubles as the consumer that proves the extracted chip components
 * work end-to-end — without a live page rendering them, the extracted
 * components would be dead code and future changes couldn't be
 * regression-tested. Pattern: consumer-driven contract. The page
 * exists partly BECAUSE the chips exist.
 */
export function StatusDashboard() {
  const { data } = useCameras();
  const cameras = data?.cameras ?? [];

  return (
    <div className={styles.wrap}>
      <GlobalHeader
        right={
          <span className={styles.blurb}>Per-camera resources, gate funnel, and VLM cost.</span>
        }
      />
      <main className={styles.main}>
        {cameras.length === 0 && (
          <div className={styles.empty}>Waiting for /api/cameras roster…</div>
        )}
        {cameras.map((camera) => (
          <section key={camera} className={styles.section}>
            <StatusBar camera={camera} />
          </section>
        ))}
      </main>
    </div>
  );
}
