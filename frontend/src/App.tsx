import { CostChip } from "./components/CostChip";

/**
 * PR 1 landing page — just proves the scaffold works end-to-end:
 * Vite build → Flask serves the bundle → CostChip fetches /status →
 * renders the vlm_cost struct we already expose. Real pages land in
 * subsequent PRs.
 */
export function App() {
  return (
    <div style={styles.wrap}>
      <header style={styles.header}>
        <span style={styles.title}>wildlife-detector — react shell</span>
        <a href="/" style={styles.link}>
          ← Back to live preview
        </a>
      </header>
      <main style={styles.main}>
        <p style={styles.blurb}>
          PR 1 scaffold. Vite + React 18 + strict TypeScript. This page renders one live widget
          wired to the real Flask API to prove the pipeline works end-to-end.
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
  link: { color: "#6bd", textDecoration: "none", marginLeft: "auto" },
  main: { padding: 24, maxWidth: 800, margin: "0 auto" },
  blurb: { color: "#8899a3", lineHeight: 1.5 },
  section: { marginTop: 32 },
  h2: { fontSize: 14, color: "#9aa", fontWeight: 500, marginBottom: 8 },
} as const;
