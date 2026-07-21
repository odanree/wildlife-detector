import { Link } from "react-router-dom";
import { StatusBar } from "../components/StatusBar";
import { useCameras } from "../hooks/useCameras";

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
    <div style={styles.wrap}>
      <header style={styles.header}>
        <span style={styles.title}>wildlife-detector — react shell</span>
        <a href="/" style={styles.link}>
          ← Live preview (vanilla-JS)
        </a>
        <Link to="/status" style={styles.link}>
          Ops dashboard →
        </Link>
        <Link to="/alerts" style={styles.link}>
          Alerts →
        </Link>
        <Link to="/baselines" style={styles.link}>
          Baselines →
        </Link>
      </header>
      <main style={styles.main}>
        <p style={styles.blurb}>
          React shell. <code>/alerts</code> and <code>/baselines</code> served here (cutovers
          complete). Live preview still on vanilla-JS — migrating next via shared header chips (PR
          7, this) then streaming preview + zone/mask canvas editors. See{" "}
          <code>docs/prototype-to-production-blueprint.md</code> Phase 7 + the migration status
          table in <code>frontend/README.md</code>.
        </p>
        <section style={styles.section}>
          <h2 style={styles.h2}>Live per-camera status</h2>
          {cameras.map((camera) => (
            <div key={camera} style={styles.cameraRow}>
              <StatusBar camera={camera} />
            </div>
          ))}
        </section>
      </main>
    </div>
  );
}

const styles = {
  wrap: {
    minHeight: "100vh",
    background: "#0e0e10",
    color: "#ddd",
    fontFamily: "-apple-system, 'Segoe UI', sans-serif",
  },
  header: {
    display: "flex",
    gap: 16,
    padding: "8px 16px",
    fontSize: 13,
    borderBottom: "1px solid #2a2a30",
    background: "#16161a",
    alignItems: "center",
  },
  title: { fontWeight: 600 },
  link: { color: "#6bd", textDecoration: "none" },
  main: { padding: 24, maxWidth: 1200, margin: "0 auto" },
  blurb: { color: "#8899a3", lineHeight: 1.5 },
  section: { marginTop: 32 },
  h2: { fontSize: 14, color: "#9aa", fontWeight: 500, marginBottom: 8 },
  cameraRow: {
    marginBottom: 12,
    padding: 8,
    background: "#131318",
    border: "1px solid #26262c",
    borderRadius: 6,
  },
} as const;
