import { Link } from "react-router-dom";
import { StatusBar } from "../components/StatusBar";
import { useCameras } from "../hooks/useCameras";

/**
 * Multi-camera operations dashboard. One StatusBar per detector.
 *
 * Doubles as the consumer that proves the extracted chip components
 * work end-to-end — without a live page rendering them, the extracted
 * components would be dead code and future changes couldn't be
 * regression-tested. Pattern: consumer-driven contract. The page
 * exists partly BECAUSE the chips exist.
 *
 * Live-preview migration in PR 8+ will reuse StatusBar above the
 * stream image on the primary/secondary panes.
 */
export function StatusDashboard() {
  const { data } = useCameras();
  const cameras = data?.cameras ?? [];

  return (
    <div style={styles.wrap}>
      <header style={styles.header}>
        <Link to="/" style={styles.title}>
          wildlife-detector — ops dashboard
        </Link>
        <span style={styles.blurb}>Per-camera resources, gate funnel, and VLM cost.</span>
        <Link to="/" style={styles.closeBtn} aria-label="Back to shell">
          ×
        </Link>
      </header>
      <main style={styles.main}>
        {cameras.length === 0 && <div style={styles.empty}>Waiting for /api/cameras roster…</div>}
        {cameras.map((camera) => (
          <section key={camera} style={styles.section}>
            <StatusBar camera={camera} />
          </section>
        ))}
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
  title: { color: "#ddd", textDecoration: "none", fontWeight: 600 },
  blurb: { color: "#8899a3" },
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
  main: { padding: 16, maxWidth: 1400, margin: "0 auto" },
  section: {
    marginBottom: 16,
    padding: 12,
    background: "#131318",
    border: "1px solid #26262c",
    borderRadius: 6,
  },
  empty: { padding: 40, textAlign: "center" as const, color: "#667" },
} as const;
