import { Link } from "react-router-dom";
import { StatusBar } from "../components/StatusBar";
import { useCameras } from "../hooks/useCameras";
import styles from "./LandingPage.module.css";

/**
 * React shell landing page. Nav + a compact per-camera status strip
 * pulled from the shared components introduced in PR 7. Kept
 * intentionally light — the ops dashboard at /status shows the same
 * chips at more depth; this page is for orientation, not monitoring.
 */
export function LandingPage() {
  const { data } = useCameras();
  const cameras = data?.cameras ?? [];
  return (
    <div className={styles.wrap}>
      <header className={styles.header}>
        <span className={styles.title}>wildlife-detector — react shell</span>
        <a href="/" className={styles.link}>
          ← Live preview (vanilla-JS)
        </a>
        <Link to="/status" className={styles.link}>
          Ops dashboard →
        </Link>
        <Link to="/alerts" className={styles.link}>
          Alerts →
        </Link>
        <Link to="/baselines" className={styles.link}>
          Baselines →
        </Link>
      </header>
      <main className={styles.main}>
        <p className={styles.blurb}>
          React shell. <code>/alerts</code> and <code>/baselines</code> served here (cutovers
          complete). Live preview still on vanilla-JS — migrating next via shared header chips (PR
          7, this) then streaming preview + zone/mask canvas editors. See{" "}
          <code>docs/prototype-to-production-blueprint.md</code> Phase 7 + the migration status
          table in <code>frontend/README.md</code>.
        </p>
        <section className={styles.section}>
          <h2 className={styles.h2}>Live per-camera status</h2>
          {cameras.map((camera) => (
            <div key={camera} className={styles.cameraRow}>
              <StatusBar camera={camera} />
            </div>
          ))}
        </section>
      </main>
    </div>
  );
}
