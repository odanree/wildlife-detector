import { Link } from "react-router-dom";
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
      <header className={styles.header}>
        <Link to="/" className={styles.title}>
          wildlife-detector — ops dashboard
        </Link>
        <span className={styles.blurb}>Per-camera resources, gate funnel, and VLM cost.</span>
        <Link to="/" className={styles.closeBtn} aria-label="Back to shell">
          ×
        </Link>
      </header>
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
