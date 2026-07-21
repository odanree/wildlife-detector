import { useStatus } from "../hooks/useStatus";

interface CostChipProps {
  camera: string;
}

/**
 * Live session cost + cache hit-rate chip. Same shape as the vanilla-JS
 * header chip in preview.py (`vlm $X.XXXX · cache YY%`) but rendered as
 * a proper React component with typed data. Meant as the PR 1 proof-of-
 * pipeline — small, self-contained, and touches every layer of the
 * scaffold (fetch → typed hook → styled component).
 */
export function CostChip({ camera }: CostChipProps) {
  const { data, error, loading } = useStatus(camera);

  if (loading && !data) return <span style={styles.chip}>vlm $— · cache —</span>;
  if (error) return <span style={{ ...styles.chip, color: "#f88" }}>error: {error.message}</span>;
  if (!data) return null;

  const cost = data.vlm_cost;
  if (!cost) {
    return <span style={styles.chip}>vlm cost tracker not present in this backend build</span>;
  }
  return (
    <span
      style={styles.chip}
      title="Session-lifetime VLM cost (USD) + prompt-cache hit rate. Cache should stay near 1.0 after warmup."
    >
      vlm <b style={styles.b}>${cost.cost_usd.toFixed(4)}</b>
      <span style={styles.sep}> · </span>
      cache <b style={styles.b}>{Math.round(cost.cache_hit_rate * 100)}%</b>
      <span style={styles.sep}> · </span>
      backend <b style={styles.b}>{data.backend}</b>
    </span>
  );
}

const styles = {
  chip: {
    display: "inline-block",
    background: "#1a1a20",
    border: "1px solid #26262c",
    borderRadius: 4,
    padding: "6px 10px",
    fontSize: 13,
    color: "#9aa",
    fontFamily: "ui-monospace, monospace",
  },
  b: { color: "#ddd", fontWeight: 600 },
  sep: { color: "#555" },
} as const;
