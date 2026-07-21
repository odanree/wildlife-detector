import { Link } from "react-router-dom";
import { CostChip } from "../components/CostChip";

/**
 * PR 1 landing page — proves the scaffold works end-to-end and links
 * to the pages already migrated. Deleted once every route has a real
 * home page (post-PR 6-ish).
 */
export function LandingPage() {
  return (
    <div style={styles.wrap}>
      <header style={styles.header}>
        <span style={styles.title}>wildlife-detector — react shell</span>
        <a href="/" style={styles.link}>
          ← Back to live preview (vanilla-JS)
        </a>
        <Link to="/alerts" style={styles.link}>
          Alerts →
        </Link>
      </header>
      <main style={styles.main}>
        <p style={styles.blurb}>
          React shell. <code>/alerts</code> now served here (cutover complete). Live preview and
          baselines still on the vanilla-JS templates — migrating in subsequent PRs. See{" "}
          <code>docs/prototype-to-production-blueprint.md</code> Phase 7 + the migration status
          table in <code>frontend/README.md</code>.
        </p>
        <section style={styles.section}>
          <h2 style={styles.h2}>Cost widget (rooftop)</h2>
          <CostChip camera="rooftop" />
        </section>
        <section style={styles.section}>
          <h2 style={styles.h2}>Cost widget (yard)</h2>
          <CostChip camera="yard" />
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
  link: { color: "#6bd", textDecoration: "none", marginLeft: 16 },
  main: { padding: 24, maxWidth: 800, margin: "0 auto" },
  blurb: { color: "#8899a3", lineHeight: 1.5 },
  section: { marginTop: 32 },
  h2: { fontSize: 14, color: "#9aa", fontWeight: 500, marginBottom: 8 },
} as const;
